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
"""

from __future__ import annotations

import json
import logging
import os
import re
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


def _build_vertex_service(engine_id: str, location_hint: Optional[str]) -> BaseSessionService:
    from google.adk.sessions import VertexAiSessionService

    class TtlVertexAiSessionService(VertexAiSessionService):
        """VertexAiSessionService that always applies a TTL, whoever creates the session."""

        def __init__(self, *args: Any, ttl_seconds: int, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self.ttl_seconds = ttl_seconds

        async def create_session(self, **kwargs: Any) -> Session:
            if kwargs.get("ttl") is None and kwargs.get("expire_time") is None:
                kwargs["ttl"] = f"{self.ttl_seconds}s"
            return await super().create_session(**kwargs)

    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
    return TtlVertexAiSessionService(
        project=project,
        location=resolve_engine_location(location_hint),
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
