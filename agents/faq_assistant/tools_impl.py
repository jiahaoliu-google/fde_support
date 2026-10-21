"""Function tools for the FAQ Assistant agent.

Both tools are plain Python functions (ADK FunctionTools), so every call shows
up as a ``function_call`` / ``function_response`` event in the agent trace.
That makes RAG usage and order lookups visible to evaluation metrics such as
``multi_turn_tool_use_quality`` and to the custom faithfulness judges.

Authentication note (Agent Identity):
    When deployed with ``--agent-identity``, the runtime provisions an X.509
    workload certificate (advertised via ``GOOGLE_API_CERTIFICATE_CONFIG``) and
    google-auth requests access tokens that are *bound* to that certificate.
    Google only accepts a bound token over mTLS using the same certificate, so
    the Discovery Engine client must be pointed at the mTLS endpoint with the
    workload certificate as its client cert. Sending the bound token over plain
    TLS fails with ``401 UNAUTHENTICATED``. See ``_get_search_client``.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import sys
from importlib import metadata as importlib_metadata

from typing import Any, Callable

from google.api_core import exceptions as gax_exceptions
from google.api_core.client_options import ClientOptions
from google.auth import exceptions as auth_exceptions
from google.cloud import discoveryengine_v1 as discoveryengine
from google.protobuf.json_format import MessageToDict

try:
    from .mock_orders import lookup_order, normalize_order_id
except ImportError:  # Loaded as a top-level module (deployed agent directory).
    from mock_orders import lookup_order, normalize_order_id

logger = logging.getLogger(__name__)
# Agent Runtime does not configure the root logger, so INFO records (e.g. the
# Discovery Engine auth-mode line) would be dropped. Attach a stdout handler for
# this module only, leaving third-party library verbosity unchanged.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

PROJECT_ID = os.getenv("GCP_PROJECT_ID") or "fde-support-sandbox-dev-jliu"
RAW_DATASTORE = os.getenv("VERTEX_AGENT_SEARCH_DATASTORE_ID", "fde-support-faq-datastore-v2")

if RAW_DATASTORE.startswith("projects/"):
    DATASTORE_PATH = RAW_DATASTORE
else:
    DATASTORE_PATH = (
        f"projects/{PROJECT_ID}/locations/global/collections/default_collection/dataStores/{RAW_DATASTORE}"
    )
SERVING_CONFIG = f"{DATASTORE_PATH}/servingConfigs/default_search"

MAX_QUERY_LENGTH = 500
DEFAULT_TOP_K = 5
MAX_TOP_K = 10
MAX_FIELD_CHARS = 300

DEFAULT_ENDPOINT = "discoveryengine.googleapis.com"
MTLS_ENDPOINT = "discoveryengine.mtls.googleapis.com"

# Environment variables read by google-auth / GAPIC clients.
_CERT_CONFIG_ENV = "GOOGLE_API_CERTIFICATE_CONFIG"
_USE_CLIENT_CERT_ENV = "GOOGLE_API_USE_CLIENT_CERTIFICATE"

_auth_mode_logged = False


def _workload_cert_paths() -> tuple[str, str] | None:
    """Returns (cert_path, key_path) of the Agent Identity workload cert, if provisioned.

    The runtime writes a JSON config of the form
    ``{"cert_configs": {"workload": {"cert_path": ..., "key_path": ...}}}`` and points
    ``GOOGLE_API_CERTIFICATE_CONFIG`` at it. Returns None when not running with Agent
    Identity (local dev, plain service account) or when the config is unusable.
    """
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


def _make_cert_source(cert_path: str, key_path: str) -> Callable[[], tuple[bytes, bytes]]:
    """Client-cert callback; re-reads the files on every call so 24h rotation is picked up."""

    def _cert_source() -> tuple[bytes, bytes]:
        with open(cert_path, "rb") as cert_file, open(key_path, "rb") as key_file:
            return cert_file.read(), key_file.read()

    return _cert_source


def _resolve_mtls_cert_paths() -> tuple[str, str] | None:
    """Decides whether to use mTLS and returns the workload cert paths if so.

    google-auth (>= 2.45) binds tokens to the workload cert whenever it is present, but
    older google-auth / GAPIC versions do not auto-enable mTLS for it. GAPIC and
    google-auth only honour a client cert when ``GOOGLE_API_USE_CLIENT_CERTIFICATE``
    resolves to true, so it is set explicitly (matching what current google-auth infers
    on its own). An explicit operator value is respected.
    """
    paths = _workload_cert_paths()
    if paths is None:
        return None
    explicit = os.getenv(_USE_CLIENT_CERT_ENV)
    if explicit is None:
        os.environ[_USE_CLIENT_CERT_ENV] = "true"
    elif explicit.strip().lower() != "true":
        logger.warning(
            "%s=%s disables mTLS although an Agent Identity certificate is present; "
            "Google APIs may reject the agent's credentials.", _USE_CLIENT_CERT_ENV, explicit,
        )
        return None
    return paths


def _package_version(name: str) -> str:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


def _log_auth_mode_once(mtls: bool, endpoint: str) -> None:
    """Logs the effective transport/auth mode once per process (never secrets)."""
    global _auth_mode_logged
    if _auth_mode_logged:
        return
    _auth_mode_logged = True
    logger.info(
        "Discovery Engine client: mtls=%s endpoint=%s google-auth=%s google-cloud-discoveryengine=%s",
        "on" if mtls else "off", endpoint,
        _package_version("google-auth"), _package_version("google-cloud-discoveryengine"),
    )


_search_client_cache: dict[tuple, discoveryengine.SearchServiceClient] = {}


def _get_search_client() -> discoveryengine.SearchServiceClient:
    """Returns a cached Discovery Engine search client matching the current auth mode.

    - Agent Identity runtime: mTLS endpoint + workload cert, REST transport. The REST
      transport's AuthorizedSession also recovers from a 401 caused by certificate
      rotation, which the gRPC transport does not.
    - Elsewhere (local ADC, service account): regular endpoint, unchanged behaviour.

    Credentials are resolved lazily by the client via ADC, so the (possibly
    certificate-bound) token always matches the certificate the mTLS channel presents.
    Caching by (client_cls, cert_paths, env_opt_out, cert_config, project_id) avoids
    repeating ADC resolution and gRPC/TLS channel setup (~0.8-1.2s) on every search call.
    """
    cert_paths = _resolve_mtls_cert_paths()
    cache_key = (
        discoveryengine.SearchServiceClient,
        cert_paths,
        os.environ.get(_USE_CLIENT_CERT_ENV),
        os.environ.get(_CERT_CONFIG_ENV),
        PROJECT_ID,
    )
    cached = _search_client_cache.get(cache_key)
    if cached is not None:
        return cached

    if cert_paths:
        _log_auth_mode_once(True, MTLS_ENDPOINT)
        # quota_project_id is intentionally not set: the REST transport ignores it, and
        # agent identities bill to their own project.
        options = ClientOptions(api_endpoint=MTLS_ENDPOINT, client_cert_source=_make_cert_source(*cert_paths))
        client = discoveryengine.SearchServiceClient(client_options=options, transport="rest")
    else:
        _log_auth_mode_once(False, DEFAULT_ENDPOINT)
        options = ClientOptions(quota_project_id=PROJECT_ID) if PROJECT_ID else None
        client = discoveryengine.SearchServiceClient(client_options=options)

    _search_client_cache[cache_key] = client
    return client


def _to_record(result: Any) -> dict:
    """Maps one SearchResult proto to the compact record returned to the LLM."""
    doc = MessageToDict(result.document._pb) if result.document else {}
    struct = doc.get("structData") or {}
    doc_name = doc.get("name", "")
    doc_id = struct.get("doc_id") or doc.get("id") or result.id or (doc_name.split("/")[-1] if doc_name else "unknown")
    return {
        "doc_id": doc_id,
        "brand": _truncate(struct.get("brand_author") or struct.get("brand") or "General"),
        "customer_question": _truncate(struct.get("customer_question")),
        "brand_response": _truncate(struct.get("reference_brand_response_text") or struct.get("brand_response")),
    }


def _truncate(value: Any) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + "..."


def search_support_records(query: str, top_k: int = DEFAULT_TOP_K) -> dict:
    """Searches the customer support knowledge base (historical support Q&A records).

    Call this ONLY when the user's request is SEARCH-READY across the conversation:
    it must include BOTH (1) a Subject / Entity Anchor (specific product, device,
    item category, brand, app, service, or region) AND (2) a Specific Intent,
    Symptom, or Policy Topic. If either element is missing (e.g. a generic
    "how do I return?" or "I need help with my iPhone"), do NOT call this tool—ask
    1 brief clarifying question first. Do NOT use this tool for the status of a
    specific order; use get_order_status for that.

    Args:
        query: A specific, self-contained search query that combines the subject
            anchor and specific intent from the conversation history, e.g.
            "AirPods disconnecting from iPhone after iOS update" or
            "computer RAM return policy restocking fee".
        top_k: Number of records to return (1-10). Defaults to 5.

    Returns:
        A dict with:
          status: "success", "no_results", "invalid_query", or "error".
          results: list of records, each with doc_id, brand, customer_question,
            and brand_response. Cite doc_id for every fact you use.
    """
    if not isinstance(query, str) or not query.strip():
        return {"status": "invalid_query", "message": "Query must be a non-empty string.", "results": []}
    clean_query = " ".join(query.split())[:MAX_QUERY_LENGTH]

    try:
        k = int(top_k)
    except (TypeError, ValueError):
        k = DEFAULT_TOP_K
    k = max(1, min(k, MAX_TOP_K))

    request = discoveryengine.SearchRequest(
        serving_config=SERVING_CONFIG,
        query=clean_query,
        page_size=k,
    )

    # Error responses deliberately carry no exception text, HTTP bodies, or credential
    # details: tool output enters the LLM context and could leak into customer answers.
    # Full diagnostics go to the server log only.
    try:
        client_fn = getattr(sys.modules.get("agents.faq_assistant.tools"), "_get_search_client", _get_search_client)
        pager = client_fn().search(request=request, timeout=12.0)
        # islice stops after k results, so no extra pages are fetched.
        raw_results = list(itertools.islice(pager, k))

    except gax_exceptions.ResourceExhausted:
        logger.warning("Datastore search rate-limited (429).")
        return {"status": "error", "error_type": "rate_limited", "results": []}
    except (gax_exceptions.Unauthenticated, auth_exceptions.GoogleAuthError) as exc:
        # 401 / credential / mTLS setup failure. Under Agent Identity this usually means a
        # certificate-bound token was sent without the matching mTLS client certificate.
        logger.error("Datastore search authentication failed: %s - %s", type(exc).__name__, exc)
        return {"status": "error", "error_type": "auth_error", "results": []}
    except gax_exceptions.PermissionDenied as exc:
        logger.error("Datastore search permission denied (check the agent identity's Discovery Engine IAM role): %s", exc)
        return {"status": "error", "error_type": "permission_denied", "results": []}
    except Exception as exc:
        logger.error("Datastore search failed: %s - %s", type(exc).__name__, exc, exc_info=True)
        return {"status": "error", "error_type": "search_unavailable", "results": []}

    if not raw_results:
        return {"status": "no_results", "query": clean_query, "results": []}
    return {"status": "success", "query": clean_query, "results": [_to_record(r) for r in raw_results]}


def get_order_status(order_id: str) -> dict:
    """Looks up the current fulfilment status of one customer order (read-only).

    Use this whenever the user asks about a specific order (where it is, when it
    arrives, whether it shipped, tracking, return or refund progress). Only call it
    when the user has provided an order ID; never guess or invent one. This tool
    cannot cancel, modify, or refund orders.

    Args:
        order_id: The order ID exactly as the user typed it in this conversation
            ("#" followed by digits; "ORD-" prefixes or a missing "#" are accepted).

    Returns:
        A dict with:
          status: "found", "not_found", or "invalid_order_id".
          order: when found, the order record with order_id, order_status, items,
            carrier, tracking_number, order_date, shipped_date,
            estimated_delivery, delivered_date, status_detail, last_update.
    """
    normalized = normalize_order_id(order_id)
    if normalized is None:
        return {
            "status": "invalid_order_id",
            "message": "Order IDs start with # followed by digits. Ask the user to confirm their order ID.",
        }

    lookup_fn = getattr(sys.modules.get("agents.faq_assistant.tools"), "lookup_order", lookup_order)
    record = lookup_fn(normalized)
    if record is None:
        return {
            "status": "not_found",
            "order_id": normalized,
            "message": "No order with this ID was found. Do not guess a status.",
        }

    # Rename to avoid confusion with the tool-level "status" key.
    record["order_status"] = record.pop("status")
    return {"status": "found", "order": record}


_DIGIT_RUN_RE = re.compile(r"\d+")


def _user_typed_texts(tool_context: Any) -> list[str]:
    """Text of every user-authored message in the session, including the current one."""
    texts: list[str] = []
    session = getattr(tool_context, "session", None)
    for event in getattr(session, "events", None) or []:
        if getattr(event, "author", None) != "user":
            continue
        for part in getattr(getattr(event, "content", None), "parts", None) or []:
            if getattr(part, "text", None):
                texts.append(part.text)
    for part in getattr(getattr(tool_context, "user_content", None), "parts", None) or []:
        if getattr(part, "text", None):
            texts.append(part.text)
    return texts


def order_id_guard(tool: Any, args: dict, tool_context: Any) -> dict | None:
    """before_tool_callback: runs get_order_status only for an order ID the user typed.

    Asked "Where is my package?" with no ID, the model called get_order_status with a
    made-up "#12345" or with "" (sometimes repeatedly), even after the example ID was
    removed from the prompt. Returning a dict skips the tool and hands this result to the
    model instead, so an invented ID can never reach the order backend.
    """
    if getattr(tool, "name", None) != "get_order_status":
        return None
    normalized = normalize_order_id((args or {}).get("order_id"))
    if normalized:
        digits = normalized.lstrip("#")
        if any(digits in _DIGIT_RUN_RE.findall(text) for text in _user_typed_texts(tool_context)):
            return None
    logger.info("Blocked get_order_status: order ID not provided by the user.")
    return {
        "status": "order_id_not_provided",
        "message": (
            "The user has not given an order ID in this conversation. Do not call "
            "get_order_status again. Ask the user for their order ID and wait for their reply."
        ),
    }
