"""Session / artifact services for the FAQ Assistant agent.

Conversation memory is stored in the Vertex AI Agent Engine Sessions API
(``VertexAiSessionService``) so it survives restarts, redeploys and scale-out
(fixes S1). Every session is created with a TTL so abandoned conversations are
purged by the platform (fixes S2 for this agent).

Engine ID resolution order (first hit wins):
    1. ``SESSION_SERVICE_URI=agentengine://<id or full resource name>``
    2. ``GOOGLE_CLOUD_AGENT_ENGINE_ID`` (injected by Agent Runtime)
    3. ``remote_agent_runtime_id`` in ``deployment_metadata.json`` (shipped with the image)

If none is found the service falls back to in-memory storage for local
development, unless ``REQUIRE_DURABLE_SESSIONS=true`` (the default on Agent
Runtime), in which case start-up fails rather than silently losing memory.

Authentication note (Agent Identity):
    With ``--agent-identity`` google-auth issues access tokens *bound* to the
    X.509 workload certificate advertised via ``GOOGLE_API_CERTIFICATE_CONFIG``.
    Google only accepts a bound token over mTLS with that same certificate, so the
    Sessions API client is pointed at the regional mTLS endpoint with an SSL context
    that presents the workload cert. Without this, every session call fails
    with ``401 UNAUTHENTICATED`` (the Discovery Engine client in ``tools_impl.py``
    applies the same fix). See ``_mtls_http_options``.

    Only the SSL context is shared, never an HTTP client: ``AdkApp`` runs each turn
    in its own ``asyncio.run`` loop, and a pooled async client reused across loops
    fails with ``... is bound to a different event loop``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
from typing import Any, Optional

from google.adk.artifacts import BaseArtifactService, InMemoryArtifactService
from google.adk.sessions import BaseSessionService, InMemorySessionService, Session

logger = logging.getLogger(__name__)

SESSION_SERVICE_URI = os.getenv("SESSION_SERVICE_URI", "memory://")
ARTIFACT_SERVICE_URI = os.getenv("ARTIFACT_SERVICE_URI", "memory://")

# Vertex sets expiry at creation time (it is not sliding). 7 days covers a normal ticket lifecycle;
# the transcript of record stays in Cloud SQL even after the agent session expires.
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 3600
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 365 * 24 * 3600

_ENGINE_RESOURCE_RE = re.compile(
    r"^projects/[A-Za-z0-9_-]+/locations/(?P<location>[A-Za-z0-9_-]+)/reasoningEngines/(?P<id>\d+)$"
)
_METADATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deployment_metadata.json")

_artifact_service = InMemoryArtifactService()
_session_service: Optional[BaseSessionService] = None

_CERT_CONFIG_ENV = "GOOGLE_API_CERTIFICATE_CONFIG"
# One SSL context per certificate generation (keyed on cert/key mtime) so the ~24h
# workload-cert rotation is picked up. SSL contexts are not bound to an event loop.
_mtls_lock = threading.Lock()
_mtls_ctx_key: Optional[tuple] = None
_mtls_ctx: Optional[ssl.SSLContext] = None


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def session_ttl_seconds() -> int:
    raw = os.getenv("FAQ_SESSION_TTL_SECONDS")
    if not raw:
        return DEFAULT_SESSION_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid FAQ_SESSION_TTL_SECONDS=%r; using default %ss.", raw, DEFAULT_SESSION_TTL_SECONDS)
        return DEFAULT_SESSION_TTL_SECONDS
    return min(max(value, _MIN_TTL_SECONDS), _MAX_TTL_SECONDS)


def _parse_engine_ref(ref: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (engine_id, location) from a numeric ID or a full resource name."""
    ref = (ref or "").strip()
    if ref.isdigit():
        return ref, None
    match = _ENGINE_RESOURCE_RE.fullmatch(ref)
    if match:
        return match.group("id"), match.group("location")
    return None, None


def resolve_agent_engine() -> tuple[Optional[str], Optional[str], str]:
    """Returns (engine_id, location_hint, source) or (None, None, reason)."""
    if SESSION_SERVICE_URI.startswith("agentengine://"):
        engine_id, location = _parse_engine_ref(SESSION_SERVICE_URI[len("agentengine://"):])
        if engine_id:
            return engine_id, location, "SESSION_SERVICE_URI"
        logger.warning("Ignoring malformed SESSION_SERVICE_URI for agentengine.")

    env_id = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID")
    if env_id:
        engine_id, location = _parse_engine_ref(env_id)
        if engine_id:
            return engine_id, location, "GOOGLE_CLOUD_AGENT_ENGINE_ID"

    # The metadata file is in the repo too; only trust it on Agent Runtime so local
    # runs don't write into the deployed engine's session store by accident.
    if _running_on_agent_runtime():
        try:
            with open(_METADATA_FILE, encoding="utf-8") as f:
                engine_id, location = _parse_engine_ref(json.load(f).get("remote_agent_runtime_id", ""))
            if engine_id:
                return engine_id, location, "deployment_metadata.json"
        except (OSError, ValueError, AttributeError):
            pass
    return None, None, "no engine id configured"


def _running_on_agent_runtime() -> bool:
    # Agent Runtime always injects the engine ID; the Agent Identity cert config is a second signal.
    return bool(os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID") or os.getenv("GOOGLE_API_CERTIFICATE_CONFIG"))


def _workload_cert_paths() -> Optional[tuple[str, str]]:
    """Returns (cert_path, key_path) of the Agent Identity workload cert, if provisioned."""
    config_path = os.getenv(_CERT_CONFIG_ENV)
    if not config_path:
        return None
    try:
        with open(config_path, encoding="utf-8") as f:
            workload = json.load(f)["cert_configs"]["workload"]
        cert_path, key_path = workload["cert_path"], workload["key_path"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Ignoring unusable certificate config %s (%s).", config_path, type(exc).__name__)
        return None
    if not all(isinstance(p, str) and os.path.isfile(p) for p in (cert_path, key_path)):
        logger.warning("Workload certificate or key referenced by %s is missing.", config_path)
        return None
    return cert_path, key_path


def _mtls_ssl_context(cert_path: str, key_path: str) -> Optional[ssl.SSLContext]:
    """SSL context presenting the workload cert; rebuilt when the cert rotates."""
    global _mtls_ctx_key, _mtls_ctx
    import certifi

    try:
        key = (cert_path, key_path, os.path.getmtime(cert_path), os.path.getmtime(key_path))
        with _mtls_lock:
            if _mtls_ctx is None or key != _mtls_ctx_key:
                ctx = ssl.create_default_context(
                    cafile=os.environ.get("SSL_CERT_FILE", certifi.where()),
                    capath=os.environ.get("SSL_CERT_DIR"),
                )
                ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
                _mtls_ctx, _mtls_ctx_key = ctx, key
            return _mtls_ctx
    except (OSError, ssl.SSLError) as exc:
        logger.warning("Cannot load workload certificate for mTLS (%s).", type(exc).__name__)
        return None


def _mtls_http_options(location: str) -> Any:
    """HttpOptions routing Sessions API calls over mTLS with the workload cert, or None.

    Returns None off Agent Identity (local ADC / service account), keeping default behaviour.
    The context is passed as client *arguments* so genai builds (and closes) a fresh
    transport per API client: ``verify`` for httpx, ``ssl`` for aiohttp (each transport
    drops the key it does not understand).
    """
    paths = _workload_cert_paths()
    ctx = _mtls_ssl_context(*paths) if paths else None
    if ctx is None:
        return None
    from google.genai import types

    return types.HttpOptions(
        base_url=f"https://{location}-aiplatform.mtls.googleapis.com/",
        async_client_args={"verify": ctx, "ssl": ctx},
    )


def _build_vertex_service(engine_id: str, location_hint: Optional[str]) -> BaseSessionService:
    from google.adk.sessions import VertexAiSessionService

    class TtlVertexAiSessionService(VertexAiSessionService):
        """VertexAiSessionService that always applies a TTL and uses mTLS under Agent Identity."""

        def __init__(self, *args: Any, ttl_seconds: int, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self.ttl_seconds = ttl_seconds

        async def create_session(self, **kwargs: Any) -> Session:
            if kwargs.get("ttl") is None and kwargs.get("expire_time") is None:
                kwargs["ttl"] = f"{self.ttl_seconds}s"
            return await super().create_session(**kwargs)

        def _api_client_http_options_override(self) -> Any:
            # Called by ADK every time it builds a Sessions API client.
            return _mtls_http_options(self._location)

    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
    location = resolve_engine_location(location_hint)
    logger.info(
        "FAQ session store transport: mtls=%s location=%s",
        "on" if _workload_cert_paths() else "off", location,
    )
    return TtlVertexAiSessionService(
        project=project,
        location=location,
        agent_engine_id=engine_id,
        ttl_seconds=session_ttl_seconds(),
    )


_APP_URL_LOCATION_RE = re.compile(r"/locations/([a-z0-9-]+)/reasoningEngines/")


def resolve_engine_location(location_hint: Optional[str] = None) -> str:
    """Region of the Agent Engine (where the Sessions API lives).

    GOOGLE_CLOUD_LOCATION is NOT used: Agent Runtime sets it to ``global`` for Gemini
    model routing, but sessions are regional and a ``global`` lookup returns 404.
    """
    candidates = [
        location_hint,
        os.getenv("AGENT_ENGINE_LOCATION"),
    ]
    match = _APP_URL_LOCATION_RE.search(os.getenv("APP_URL", ""))
    if match:
        candidates.append(match.group(1))
    for loc in candidates:
        if loc and loc != "global":
            return loc
    return "us-central1"


def build_session_service() -> BaseSessionService:
    engine_id, location_hint, source = resolve_agent_engine()
    if engine_id:
        logger.info(
            "FAQ session store: VertexAiSessionService (engine=%s, source=%s, ttl=%ss).",
            engine_id, source, session_ttl_seconds(),
        )
        return _build_vertex_service(engine_id, location_hint)

    require_durable = _truthy(os.getenv("REQUIRE_DURABLE_SESSIONS", "true" if _running_on_agent_runtime() else "false"))
    if require_durable:
        raise RuntimeError(
            "Durable sessions are required but no Agent Engine ID was found "
            "(set GOOGLE_CLOUD_AGENT_ENGINE_ID or SESSION_SERVICE_URI=agentengine://<id>)."
        )
    logger.warning("FAQ session store: InMemorySessionService (%s). Conversation memory is NOT durable.", source)
    return InMemorySessionService()


def get_session_service() -> BaseSessionService:
    """Builder passed to AdkApp; returns a process-wide singleton."""
    global _session_service
    if _session_service is None:
        _session_service = build_session_service()
    return _session_service


def get_artifact_service() -> BaseArtifactService:
    return _artifact_service


async def ensure_session(app_name: str, user_id: str, session_id: str) -> None:
    """Creates the session (with TTL) if it does not exist yet.

    Done explicitly so behaviour does not depend on the ADK Runner's
    ``auto_create_session`` default, which differs between versions.
    """
    service = get_session_service()
    existing = await service.get_session(app_name=app_name, user_id=user_id, session_id=session_id)
    if existing is None:
        await service.create_session(app_name=app_name, user_id=user_id, session_id=session_id)


async def after_stream(app_name: str, user_id: str, session_id: str) -> None:
    """FAQ sessions are multi-turn: keep them (TTL / ticket resolution clean them up)."""
    return None
