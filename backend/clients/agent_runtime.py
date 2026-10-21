"""HTTP calls to the Sentiment and FAQ agents on Vertex AI Agent Runtime.

Also owns the FAQ circuit breaker, the FAQ retry policy and agent session IDs.
Tests patch names on this module (e.g. call_sentiment_agent, httpx, FAQ_AGENT_URL);
callers elsewhere must use `agent_runtime.<name>` rather than importing the bare
name, so a patch here is always seen.
"""
import asyncio
import json
import logging
import os
import random
import re
import uuid

import httpx
from fastapi import HTTPException

from backend.clients import gcp_auth, stream_parsing
from backend.core.circuit_breaker import CircuitBreaker
from backend.core.config import settings
from backend.core.telemetry import tracer

logger = logging.getLogger("backend")

# Configurable Agent Runtime Microservice Endpoints on Vertex AI
SENTIMENT_AGENT_URL = settings.SENTIMENT_AGENT_URL
FAQ_AGENT_URL = settings.FAQ_AGENT_URL

# Instantiate CircuitBreaker from settings
rag_circuit_breaker = CircuitBreaker(
    failure_threshold=settings.CIRCUIT_FAILURE_THRESHOLD,
    recovery_timeout_seconds=settings.CIRCUIT_RECOVERY_TIMEOUT
)

# Must match SentimentResult.sentiment in agents/sentiment_router/agent.py.
SENTIMENT_URGENT = "URGENT_FRUSTRATED"
SENTIMENT_NEUTRAL = "NEUTRAL_FACTUAL"
SENTIMENT_LABELS = frozenset({SENTIMENT_URGENT, SENTIMENT_NEUTRAL})

class SentimentClassificationError(Exception):
    """The agent answered, but not with a usable label. Fails closed (escalate), and is
    not counted as an availability failure by the circuit breaker."""

class UpstreamDependencyError(Exception):
    """The FAQ agent replied, but Agent Search failed during the turn.

    The search tool catches Discovery Engine errors and hands them to the model, so the
    agent endpoint still returns HTTP 200. Raising this makes the backend treat the turn
    like an HTTP error from the agent: the circuit breaker counts it, and the customer
    gets the outage fallback.
    """

    def __init__(self, status_code: int, reason: str):
        super().__init__(f"Agent Search {reason} (HTTP {status_code})")
        self.status_code = status_code
        self.reason = reason

# FAQ agent retry policy (scope: exponential backoff). Retries happen inside one customer
# question; the circuit breaker gets a single outcome per question, after all attempts.
FAQ_MAX_ATTEMPTS = 3
FAQ_RETRY_BASE_DELAY_SECONDS = 0.5
FAQ_RETRY_MAX_DELAY_SECONDS = 2.0

def faq_failure_status(exc: Exception) -> int:
    """HTTP-style status for a failed FAQ agent attempt (timeouts count as 504)."""
    if isinstance(exc, httpx.TimeoutException):
        return 504
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else 503

def faq_is_retryable(status_code: int) -> bool:
    """Only transient server errors are retried.

    429: retrying adds load to an exhausted quota. 504: the timeout already used the
    request's time budget. Other 4xx: the same request fails the same way again.
    """
    return status_code >= 500 and status_code != 504

def faq_retry_delay(attempt: int) -> float:
    """Exponential backoff with full jitter: uniform(0, min(max, base * 2^(attempt-1)))."""
    cap = min(FAQ_RETRY_MAX_DELAY_SECONDS, FAQ_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    return random.uniform(0, cap)

async def query_faq_with_retries(message: str, session_id: str, user_id: str,
                                 max_attempts: int) -> tuple[dict | None, int | None]:
    """Calls the FAQ agent for one customer question.

    Returns (faq_data, None) on success, or (None, status_code) of the last failed attempt.
    """
    last_status = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await call_faq_agent(message, session_id, user_id), None
        except Exception as e:
            last_status = faq_failure_status(e)
            logger.warning(f"FAQ agent attempt {attempt}/{max_attempts} failed (status={last_status}): {e}")
            if attempt == max_attempts or not faq_is_retryable(last_status):
                break
            await asyncio.sleep(faq_retry_delay(attempt))
    return None, last_status

async def call_sentiment_agent(message: str) -> dict:
    """Sends HTTP REST request over the network to Sentiment Agent on Agent Runtime."""
    if not SENTIMENT_AGENT_URL:
        raise HTTPException(status_code=500, detail="SENTIMENT_AGENT_URL is not configured.")

    headers = gcp_auth.get_gcp_auth_headers()
    headers["Content-Type"] = "application/json"
    
    # Fresh session per classification: classification is stateless, and a shared
    # session would mix every customer's messages into one conversation history.
    # The agent deletes the session when the run ends (see sentiment app_utils/services.py).
    payload = {
        "class_method": "stream_query",
        "input": {
            "user_id": "sentiment_classifier",
            "session_id": f"s-{uuid.uuid4().hex}",
            "message": message
        }
    }

    with tracer.start_as_current_span("call_sentiment_agent_runtime") as span:
        span.set_attribute("agent.url", SENTIMENT_AGENT_URL)
        span.set_attribute("input.message_length", len(message))

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(SENTIMENT_AGENT_URL, json=payload, headers=headers)
            span.set_attribute("http.status_code", resp.status_code)

            if resp.status_code == 200:
                full_text, _ = stream_parsing.parse_ndjson_stream(resp.text)
                clean_text = full_text.strip()
                if "```json" in clean_text:
                    clean_text = clean_text.split("```json")[-1].split("```")[0].strip()
                elif "```" in clean_text:
                    clean_text = clean_text.split("```")[1].strip()
                
                try:
                    result = json.loads(clean_text)
                except Exception:
                    span.set_attribute("sentiment.classified", "UNPARSEABLE")
                    raise SentimentClassificationError("Sentiment agent returned non-JSON output")

                label = result.get("sentiment") if isinstance(result, dict) else None
                if label not in SENTIMENT_LABELS:
                    span.set_attribute("sentiment.classified", "INVALID_LABEL")
                    raise SentimentClassificationError(f"Unknown sentiment label: {str(label)[:40]!r}")

                span.set_attribute("sentiment.classified", label)
                return result
                    
            logger.error(f"Sentiment Agent HTTP {resp.status_code}: {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=f"Sentiment Agent Runtime error: {resp.text}")

async def call_faq_agent(message: str, session_id: str, user_id: str = "backend_user") -> dict:
    """Sends HTTP REST request over the network to FAQ Agent on Agent Runtime."""
    if not FAQ_AGENT_URL:
        raise HTTPException(status_code=500, detail="FAQ_AGENT_URL is not configured.")

    headers = gcp_auth.get_gcp_auth_headers()
    headers["Content-Type"] = "application/json"
    
    # user_id scopes the agent's conversation memory per customer, so a leaked
    # session_id alone cannot join another customer's conversation.
    payload = {
        "class_method": "stream_query",
        "input": {
            "user_id": user_id,
            "session_id": agent_session_id(session_id),
            "message": message
        }
    }

    with tracer.start_as_current_span("call_faq_agent_runtime") as span:
        span.set_attribute("agent.url", FAQ_AGENT_URL)
        span.set_attribute("session.id", session_id)
        span.set_attribute("input.message_length", len(message))

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(FAQ_AGENT_URL, json=payload, headers=headers)
            span.set_attribute("http.status_code", resp.status_code)

            if resp.status_code == 200:
                # Agent Search errors are caught inside the tool, so they arrive as HTTP 200.
                # Surface them here so the circuit breaker sees them (TDD: open on 429/503).
                search_failure = stream_parsing.detect_search_tool_failure(resp.text)
                if search_failure:
                    code, reason = search_failure
                    span.set_attribute("faq.search_failure", reason)
                    logger.warning(f"FAQ Agent turn hit an Agent Search failure: {reason} (HTTP {code})")
                    raise UpstreamDependencyError(code, reason)

                answer_text, citations = stream_parsing.parse_ndjson_stream(resp.text)
                grounded = bool(answer_text and len(answer_text.strip()) > 0)
                span.set_attribute("faq.grounded", grounded)
                span.set_attribute("faq.citation_count", len(citations))

                if grounded:
                    return {"answer": answer_text, "citations": citations, "grounded": True}
                
                # Graceful Fallback: Model returned empty answer / thought-only response.
                # The message says the question was routed to a human, so escalate for real.
                logger.warning(f"FAQ Agent returned 200 but empty answer_text for query: '{message}'")
                return {
                    "answer": "I don't have specific information on that right now, so I've routed your question to our support team for further assistance.",
                    "citations": ["FAQ Datastore Search"],
                    "grounded": False,
                    "escalate": True,
                }

            # Graceful Fallback for Vertex AI Agent Engine model-level empty / thought errors
            if "Model returned no actionable content" in resp.text or "thought-only" in resp.text:
                logger.warning(f"FAQ Agent returned model-level empty content error ({resp.status_code}): {resp.text}")
                span.set_attribute("faq.model_error", "thought_only_fallback")
                return {
                    "answer": "I don't have specific information on that right now, so I've routed your question to our support team for further assistance.",
                    "citations": ["FAQ Datastore Search"],
                    "grounded": False,
                    "escalate": True,
                }

            logger.error(f"FAQ Agent HTTP {resp.status_code}: {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=f"FAQ Agent Runtime error: {resp.text}")

# Vertex AI Agent Engine session IDs: lowercase [a-z0-9-], start with a letter, <= 63 chars.
_VERTEX_SESSION_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")

DEFAULT_FAQ_SESSION_TTL_SECONDS = 7 * 24 * 3600

def faq_session_ttl_seconds() -> int:
    """Session TTL in seconds on the FAQ agent (matches agents/faq_assistant/app_utils/services.py)."""
    raw = os.environ.get("FAQ_SESSION_TTL_SECONDS", "").strip()
    if raw:
        try:
            return max(60, min(int(raw), 365 * 24 * 3600))
        except ValueError:
            logger.warning(
                "Ignoring non-integer FAQ_SESSION_TTL_SECONDS=%r; using %ds.",
                raw,
                DEFAULT_FAQ_SESSION_TTL_SECONDS,
            )
    return DEFAULT_FAQ_SESSION_TTL_SECONDS

def agent_session_id(ticket_id: str) -> str:
    """Maps a ticket ID (uuid4, may start with a digit) to a valid, deterministic agent session ID."""
    sid = f"t-{ticket_id.strip().lower()}"
    if not _VERTEX_SESSION_ID_RE.fullmatch(sid):
        raise HTTPException(status_code=400, detail="Invalid ticket ID")
    return sid

def faq_agent_query_url() -> str:
    """Derives the engine's non-streaming ':query' URL from FAQ_AGENT_URL (':streamQuery')."""
    if FAQ_AGENT_URL.endswith(":streamQuery"):
        return FAQ_AGENT_URL[: -len(":streamQuery")] + ":query"
    return FAQ_AGENT_URL

async def list_faq_sessions(user_id: str) -> set[str] | None:
    """Fetches active FAQ agent session IDs (e.g. {'t-<uuid>', ...}) for a user.

    Returns None when FAQ_AGENT_URL is unconfigured or the Agent Runtime call fails,
    so callers can distinguish "agent platform unreachable" from "session was evicted".
    """
    if not FAQ_AGENT_URL:
        return None
    with tracer.start_as_current_span("list_faq_agent_sessions") as span:
        try:
            payload = {
                "class_method": "async_list_sessions",
                "input": {"user_id": user_id},
            }
            headers = gcp_auth.get_gcp_auth_headers()
            headers["Content-Type"] = "application/json"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(faq_agent_query_url(), json=payload, headers=headers)
            span.set_attribute("http.status_code", resp.status_code)
            if resp.status_code != 200:
                logger.warning("FAQ session list returned HTTP %s.", resp.status_code)
                return None
            body = resp.json()
            output = body.get("output", body) if isinstance(body, dict) else {}
            raw_sessions = output.get("sessions", []) if isinstance(output, dict) else []
            active: set[str] = set()
            for s in raw_sessions:
                if isinstance(s, dict):
                    sid = s.get("id") or s.get("session_id") or s.get("name", "")
                    if isinstance(sid, str) and sid:
                        active.add(sid.rsplit("/", 1)[-1])
            span.set_attribute("faq.active_sessions", len(active))
            return active
        except Exception as e:
            logger.warning("FAQ session list failed: %s", type(e).__name__)
            return None

async def delete_faq_session(ticket_id: str, user_id: str) -> bool:
    """Best-effort deletion of the FAQ agent's conversation memory for a ticket.

    Never raises: the 7-day session TTL on the agent side is the backstop.
    """
    if not FAQ_AGENT_URL:
        return False
    with tracer.start_as_current_span("delete_faq_agent_session") as span:
        span.set_attribute("session.id", ticket_id)
        try:
            payload = {
                "class_method": "async_delete_session",
                "input": {"user_id": user_id, "session_id": agent_session_id(ticket_id)},
            }
            headers = gcp_auth.get_gcp_auth_headers()
            headers["Content-Type"] = "application/json"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(faq_agent_query_url(), json=payload, headers=headers)
            span.set_attribute("http.status_code", resp.status_code)
            if resp.status_code == 200:
                return True
            logger.warning("FAQ session cleanup for ticket %s returned HTTP %s.", ticket_id, resp.status_code)
        except Exception as e:
            logger.warning("FAQ session cleanup for ticket %s failed: %s", ticket_id, type(e).__name__)
        return False
