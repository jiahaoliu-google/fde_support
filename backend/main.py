import os
import re
import json
import uuid
import hashlib
import logging
import asyncio
import random
import threading
import certifi
import httpx
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.config import settings
from backend.caller_identity import CallerAuthError, build_caller_resolver
from backend.circuit_breaker import CircuitBreaker, CircuitState
from backend.model_armor import model_armor
from agents.faq_assistant.answer_sanitizer import sanitize_answer
from database.db import init_db, get_db
from database.models import Ticket, Message

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

logger = logging.getLogger("backend")
logging.basicConfig(level=logging.INFO if settings.DEBUG else logging.WARNING)

# Ensure database tables exist on module import / startup.
# Production fails fast: if the DB is unreachable the container exits, so Cloud Run never
# routes traffic to the revision (the previous healthy revision keeps serving).
try:
    init_db()
except Exception as e:
    if settings.is_production:
        logger.critical(f"Database initialization failed in production; refusing to start: {type(e).__name__}: {e}")
        raise
    logger.warning(f"Initial DB check warning: {e}")

# Configurable Agent Runtime Microservice Endpoints on Vertex AI
SENTIMENT_AGENT_URL = settings.SENTIMENT_AGENT_URL
FAQ_AGENT_URL = settings.FAQ_AGENT_URL

# Instantiate CircuitBreaker from settings
rag_circuit_breaker = CircuitBreaker(
    failure_threshold=settings.CIRCUIT_FAILURE_THRESHOLD,
    recovery_timeout_seconds=settings.CIRCUIT_RECOVERY_TIMEOUT
)

_gcp_credentials = None
_gcp_credentials_lock = threading.Lock()

def get_gcp_auth_headers() -> dict:
    """Returns an OAuth access-token header for calling Reasoning Engine endpoints.

    Credentials are loaded once and refreshed only when expired or about to expire
    (google-auth's ``valid`` includes a refresh margin), so a normal request does no
    blocking network I/O here.
    """
    global _gcp_credentials
    try:
        import google.auth
        import google.auth.transport.requests
        with _gcp_credentials_lock:
            if _gcp_credentials is None:
                _gcp_credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
            if not _gcp_credentials.valid:
                _gcp_credentials.refresh(google.auth.transport.requests.Request())
            return {"Authorization": f"Bearer {_gcp_credentials.token}"}
    except Exception as e:
        logger.warning(f"GCP Auth token acquisition failed: {type(e).__name__}: {e}")
        return {}

def parse_ndjson_stream(raw_text: str) -> tuple[str, list[str]]:
    """Parses NDJSON streamed response from Reasoning Engine endpoints."""
    full_text = ""
    citations = []
    
    for line in raw_text.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if "content" in data and "parts" in data["content"]:
                event_text = ""
                for part in data["content"]["parts"]:
                    if part.get("thought"):
                        continue
                    event_text += part.get("text", "") or ""

                    # Citations from FunctionTool responses (search_support_records / get_order_status)
                    fr = part.get("function_response") or part.get("functionResponse") or {}
                    fr_resp = fr.get("response") or {}
                    if fr.get("name") == "search_support_records":
                        for idx, rec in enumerate(fr_resp.get("results") or [], 1):
                            doc_id = rec.get("doc_id") if isinstance(rec, dict) else None
                            if doc_id:
                                c_entry = f"Document #{idx} (ID: {doc_id})"
                                if c_entry not in citations:
                                    citations.append(c_entry)
                        if fr_resp.get("query"):
                            c_entry = f"Datastore Search: {fr_resp['query']}"
                            if c_entry not in citations:
                                citations.append(c_entry)
                    elif fr.get("name") == "get_order_status" and fr_resp.get("status") == "found":
                        oid = (fr_resp.get("order") or {}).get("order_id")
                        if oid and f"Order Lookup: {oid}" not in citations:
                            citations.append(f"Order Lookup: {oid}")

                # Keep only the latest model text: earlier events may contain pre-tool-call text
                if event_text.strip():
                    full_text = event_text
            
            # Extract grounding metadata citations
            gm = data.get("grounding_metadata") or data.get("groundingMetadata") or {}
            if gm:
                chunks = gm.get("grounding_chunks") or gm.get("groundingChunks") or []
                for idx, chunk in enumerate(chunks, 1):
                    rc = chunk.get("retrieved_context") or chunk.get("retrievedContext") or {}
                    web = chunk.get("web") or {}
                    doc_path = (
                        rc.get("document")
                        or rc.get("document_name")
                        or rc.get("documentName")
                        or rc.get("uri")
                        or rc.get("title")
                        or web.get("title")
                        or web.get("uri")
                    )
                    if doc_path:
                        # Combine Document Ref index and Document ID into single record
                        if "/documents/" in str(doc_path):
                            doc_id = str(doc_path).split("/documents/")[-1]
                            c_entry = f"Document #{idx} (ID: {doc_id})"
                        else:
                            c_entry = f"Document #{idx} (ID: {doc_path})"
                    else:
                        c_entry = f"Document #{idx}"
                    if c_entry not in citations:
                        citations.append(c_entry)
                
                ret_queries = gm.get("retrieval_queries") or gm.get("retrievalQueries") or []
                for q in ret_queries:
                    c_entry = f"Datastore Search: {q}"
                    if c_entry not in citations:
                        citations.append(c_entry)
                        
                web_queries = gm.get("web_search_queries") or gm.get("webSearchQueries") or []
                for q in web_queries:
                    c_entry = f"Web Search: {q}"
                    if c_entry not in citations:
                        citations.append(c_entry)
        except Exception:
            pass

    return full_text, citations

def format_citations(raw_citations: list[str]) -> list[str]:
    """Normalizes raw citation references into descriptive Document Names/IDs for UI rendering."""
    formatted = []
    for c in raw_citations:
        c_str = str(c).strip()
        if not c_str:
            continue
        if "(ID:" in c_str and not ("/documents/" in c_str or ": projects/" in c_str):
            entry = c_str
        elif "/documents/" in c_str:
            doc_id = c_str.split("/documents/")[-1].strip()
            if c_str.startswith("Document #") or c_str.startswith("Document#"):
                doc_num = c_str.split(":")[0].strip()
                entry = f"{doc_num} (ID: {doc_id})"
            elif c_str.startswith("Datastore Document Ref #"):
                ref = c_str.split(":")[0].replace("Datastore Document Ref #", "").strip()
                entry = f"Document #{ref} (ID: {doc_id})"
            else:
                entry = f"Document (ID: {doc_id})"
        elif c_str.startswith("Datastore Document Ref #"):
            ref = c_str.replace("Datastore Document Ref #", "").strip()
            entry = f"Document #{ref}"
        elif c_str.startswith("Datastore Doc ID:"):
            doc_id = c_str.replace("Datastore Doc ID:", "").strip()
            entry = f"Document (ID: {doc_id})"
        elif c_str.isdigit():
            entry = f"Document #{c_str}"
        elif c_str.startswith("http://") or c_str.startswith("https://") or c_str.startswith("gs://"):
            fname = c_str.split("/")[-1] or c_str
            entry = f"Doc: {fname} ({c_str})"
        else:
            entry = c_str

        if entry not in formatted:
            formatted.append(entry)
    return formatted

# Must match SentimentResult.sentiment in agents/sentiment_router/agent.py.
SENTIMENT_URGENT = "URGENT_FRUSTRATED"
SENTIMENT_NEUTRAL = "NEUTRAL_FACTUAL"
SENTIMENT_LABELS = frozenset({SENTIMENT_URGENT, SENTIMENT_NEUTRAL})

class SentimentClassificationError(Exception):
    """The agent answered, but not with a usable label. Fails closed (escalate), and is
    not counted as an availability failure by the circuit breaker."""

# search_support_records error types (agents/faq_assistant/tools.py) that mean Agent Search
# itself is unhealthy, mapped to the HTTP status the circuit breaker should record.
# auth_error / permission_denied are configuration faults that won't recover on their own,
# so they are left to the agent's normal escalate=true answer and don't trip the breaker.
SEARCH_TOOL_ERROR_STATUS = {"rate_limited": 429, "search_unavailable": 503}

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

def detect_search_tool_failure(raw_text: str) -> tuple[int, str] | None:
    """Returns (status_code, error_type) if Agent Search failed during this agent turn.

    Reads the search_support_records function_response events in the NDJSON stream.
    Returns None if no search ran, if any search in the turn succeeded (the answer can
    still be grounded), or if the only errors are non-transient ones. A 429 wins over
    a 503 when both appear.
    """
    failure = None
    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        content = data.get("content") if isinstance(data, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            fr = part.get("function_response") or part.get("functionResponse")
            if not isinstance(fr, dict) or fr.get("name") != "search_support_records":
                continue
            resp = fr.get("response")
            status = resp.get("status") if isinstance(resp, dict) else None
            if status in ("success", "no_results"):
                return None
            if status == "error":
                error_type = resp.get("error_type")
                code = SEARCH_TOOL_ERROR_STATUS.get(error_type)
                if code and (failure is None or code == 429):
                    failure = (code, error_type)
    return failure

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

    headers = get_gcp_auth_headers()
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
                full_text, _ = parse_ndjson_stream(resp.text)
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

    headers = get_gcp_auth_headers()
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
                search_failure = detect_search_tool_failure(resp.text)
                if search_failure:
                    code, reason = search_failure
                    span.set_attribute("faq.search_failure", reason)
                    logger.warning(f"FAQ Agent turn hit an Agent Search failure: {reason} (HTTP {code})")
                    raise UpstreamDependencyError(code, reason)

                answer_text, citations = parse_ndjson_stream(resp.text)
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
            headers = get_gcp_auth_headers()
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

app = FastAPI(
    title="FDE Support Decoupled App Backend",
    description="App Backend Server communicating with Agent Runtime Reasoning Engines.",
    version="2.5.0"
)

# No CORSMiddleware: the backend is only invoked server-to-server by the frontend proxy
# (Cloud Run IAM), never directly by browsers, so no cross-origin access is granted.

# OpenTelemetry Cloud Trace Instrumentation
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME

    resource = Resource.create({
        SERVICE_NAME: "fde-support-backend",
        "service.name": "fde-support-backend",
        "service.namespace": "fde-support",
        "service.version": "2.5.0"
    })

    tracer_provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(tracer_provider)

    # CRITICAL: Pass resource_regex=".*" so opentelemetry-exporter-gcp-trace exports custom resource attributes
    cloud_trace_exporter = CloudTraceSpanExporter(resource_regex=".*")
    tracer_provider.add_span_processor(BatchSpanProcessor(cloud_trace_exporter))

    tracer = trace.get_tracer("fde-support-backend")

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    logger.info("OpenTelemetry Cloud Trace instrumentation initialized with resource_regex='.*' and service.name=fde-support-backend.")
except Exception as e:
    logger.warning(f"OpenTelemetry Cloud Trace setup skipped or fallback: {e}")
    from opentelemetry import trace
    tracer = trace.get_tracer("fde-support-backend")


# ---------------------------------------------------------------------------
# Caller identity & ticket ownership
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,253}$")

_caller_resolver = build_caller_resolver(
    iap_audience=settings.IAP_AUDIENCE,
    frontend_service_account=settings.FRONTEND_SERVICE_ACCOUNT,
    allow_direct_callers=settings.ALLOW_DIRECT_CALLERS,
    dev_email=settings.DEV_FAKE_USER_EMAIL,
)

async def get_caller(request: Request) -> str:
    """Returns the authenticated end-user identity (see backend/caller_identity.py).

    Identity comes only from the IAP-signed JWT forwarded by the frontend (verified
    here), or from a direct caller's own Cloud Run-verified ID token. X-User-Email and
    identity fields in request bodies are ignored.
    """
    try:
        caller = await _caller_resolver.resolve(request.headers)
    except CallerAuthError as e:
        logger.warning(f"Rejected request to {request.url.path}: {e}")
        raise HTTPException(status_code=401, detail="Authentication required")
    except Exception as e:
        logger.error(f"Caller identity error on {request.url.path}: {type(e).__name__}")
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _EMAIL_RE.match(caller.email):
        raise HTTPException(status_code=401, detail="Authentication required")
    logger.info(f"Caller authenticated via {caller.source} for {request.url.path}")
    return caller.email

def agent_user_id(caller: str) -> str:
    """Stable, non-reversible per-user key for Agent Runtime sessions (no raw email sent)."""
    return "u_" + hashlib.sha256(caller.encode("utf-8")).hexdigest()[:24]

def new_ticket_id() -> str:
    return str(uuid.uuid4())

def get_owned_ticket(db: Session, ticket_id: str, caller: str, *, for_update: bool = False) -> Ticket:
    """Fetches a ticket only if it belongs to the caller. 404 (not 403) avoids revealing existence.

    for_update=True locks the row until the current transaction ends (SELECT ... FOR UPDATE on
    Postgres; SQLite has no row locks and ignores it). Use it when the status is about to be
    read and then written, so concurrent requests on the same ticket are serialized.
    """
    query = db.query(Ticket).filter(Ticket.id == ticket_id, Ticket.customer_id == caller)
    if for_update:
        query = query.with_for_update()
    ticket = query.first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


# ---------------------------------------------------------------------------
# Ticket transactions
#
# Rules for /api/chat:
# * Every DB step is one explicit, short `with db.begin():` block, so its writes are atomic.
# * No transaction or pooled connection is held while waiting on an agent (sentiment or FAQ).
#   Values needed after a block are copied into locals inside it; touching an ORM object
#   after its commit would reload it and silently open a new transaction.
# * Each write re-reads the ticket under a row lock, because the status may have changed
#   during the agent call (another tab resolved or escalated it).
# ---------------------------------------------------------------------------
TICKET_RESOLVED_DETAIL = (
    "This ticket is marked as RESOLVED. Please click 'Start New Ticket' to submit new inquiries."
)
# Must contain "RESOLVED": the UI locks the chat on a 400 whose detail includes it.
TICKET_RESOLVED_DURING_REQUEST_DETAIL = (
    "This ticket was marked as RESOLVED while your message was being processed. "
    "Please click 'Start New Ticket' to submit new inquiries."
)

def record_turn(db: Session, ticket_id: str, caller: str, sender: str, text: str, *, escalate: bool) -> str:
    """Appends one message and applies the status change in a single transaction.

    The ticket is re-read under a row lock:
    * RESOLVED: nothing is written and a 400 is raised (a resolved ticket is never reopened).
    * ESCALATED: stays ESCALATED (never downgraded).
    * escalate=True: becomes ESCALATED. NEW becomes IN_PROGRESS otherwise.
    Returns the resulting status.
    """
    with db.begin():
        ticket = get_owned_ticket(db, ticket_id, caller, for_update=True)
        if ticket.current_status == "RESOLVED":
            raise HTTPException(status_code=400, detail=TICKET_RESOLVED_DURING_REQUEST_DETAIL)
        if escalate:
            ticket.current_status = "ESCALATED"
        elif ticket.current_status == "NEW":
            ticket.current_status = "IN_PROGRESS"
        db.add(Message(ticket_id=ticket_id, sender=sender, message_text=text))
        status = ticket.current_status
    return status


class CreateTicketRequest(BaseModel):
    # Deprecated/ignored: ticket IDs are generated server-side and identity comes from auth.
    session_id: str | None = None
    customer_id: str | None = None

class SupportQueryRequest(BaseModel):
    message: str = Field(..., max_length=4000, example="Where is my order #12345?")
    # Ticket ID returned by /api/tickets/create. Omit to open a new ticket.
    session_id: str | None = Field(default=None, max_length=64, example="3f0c...uuid")
    # Deprecated/ignored: identity comes from the authenticated caller.
    customer_id: str | None = None

class SupportQueryResponse(BaseModel):
    ticket_id: str
    ticket_status: str
    answer: str
    sentiment: str
    routed_to: str
    extracted_entities: dict
    citations: list[str]
    grounded: bool
    session_id: str
    agent_service_version: str = "v2.5-cloud-sql-sessions"

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "FDE Support Decoupled App Backend",
        "circuit_breaker_state": rag_circuit_breaker.current_state.value,
        "database": "connected",
        "version": "2.5.0"
    }

@app.get("/api/status")
async def get_services_status():
    return {
        "backend_service": {"status": "online", "port": 8000},
        "circuit_breaker": {
            "state": rag_circuit_breaker.current_state.value,
            "failure_count": rag_circuit_breaker.failure_count
        },
        "database": {"status": "active"},
        "agent_runtime_microservice": {
            "status": "online",
            "faq_agent_url": FAQ_AGENT_URL,
            "sentiment_agent_url": SENTIMENT_AGENT_URL
        }
    }

ESCALATED_FOLLOWUP_ACK = (
    "Your ticket is with our human support team. We've added your message to it, "
    "and a specialist will reply here."
)

def escalated_followup_response(ticket_id: str, sentiment: str | None) -> "SupportQueryResponse":
    # The ack is returned to the UI but not stored, so the human's transcript holds only
    # real customer turns.
    return SupportQueryResponse(
        ticket_id=ticket_id,
        ticket_status="ESCALATED",
        answer=ESCALATED_FOLLOWUP_ACK,
        sentiment=sentiment or "UNAVAILABLE",
        routed_to="Human Escalation Queue",
        extracted_entities={},
        citations=["Assigned to Human Support Specialist Queue"],
        grounded=False,
        session_id=ticket_id,
        agent_service_version="v2.5-human-escalation",
    )

def append_to_escalated_ticket(db: Session, ticket_id: str, caller: str, message: str) -> "SupportQueryResponse":
    """Records a customer follow-up for the human handling an ESCALATED ticket. No LLM calls."""
    with tracer.start_as_current_span("escalated_ticket_followup") as span:
        span.set_attribute("ticket.id", ticket_id)
        with db.begin():
            ticket = get_owned_ticket(db, ticket_id, caller, for_update=True)
            if ticket.current_status == "RESOLVED":  # resolved since the ownership check
                raise HTTPException(status_code=400, detail=TICKET_RESOLVED_DETAIL)
            db.add(Message(ticket_id=ticket_id, sender="CUSTOMER", message_text=message))
            sentiment = ticket.sentiment
    return escalated_followup_response(ticket_id, sentiment)


@app.post("/api/chat", response_model=SupportQueryResponse)
async def process_support_query(request: SupportQueryRequest, db: Session = Depends(get_db), caller: str = Depends(get_caller)):
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # Ownership check BEFORE any LLM call: a foreign/unknown ticket ID is rejected,
    # so callers can never append to (or read agent memory of) another user's ticket.
    # Read-only transaction: it ends here, before any agent call.
    existing_status = None
    if request.session_id:
        with db.begin():
            existing_status = get_owned_ticket(db, request.session_id, caller).current_status
        if existing_status == "RESOLVED":
            raise HTTPException(status_code=400, detail=TICKET_RESOLVED_DETAIL)
    else:
        request.session_id = new_ticket_id()
    request.customer_id = caller
    ticket_id = request.session_id

    # Step 0: Model Armor / Sensitive Data Protection Redaction
    # Redacts Credit Card Numbers, SSNs, and Email Addresses before sending to Gemini agents
    with tracer.start_as_current_span("model_armor_redact") as span:
        original_msg = request.message
        request.message = model_armor.redact(request.message)
        redacted = (request.message != original_msg)
        span.set_attribute("model_armor.redacted", redacted)
        if redacted:
            logger.info("Model Armor successfully redacted sensitive customer PII before LLM dispatch.")

    # Escalated tickets belong to the human queue (scoping doc): no sentiment or FAQ call.
    # This is also what keeps the FAQ agent's memory consistent with the transcript: every
    # turn it never saw ends with the ticket ESCALATED, and it is never asked again.
    if existing_status == "ESCALATED":
        return append_to_escalated_ticket(db, ticket_id, caller, request.message)

    # Step 1: Call Sentiment Agent on Agent Runtime over HTTP.
    # Fail closed: if we can't get a valid label, the ticket goes to a human.
    sentiment = SENTIMENT_NEUTRAL
    entities = {}
    sentiment_failed = False
    
    try:
        s_data = await call_sentiment_agent(request.message)
        sentiment = s_data["sentiment"]  # validated against SENTIMENT_LABELS
        
        extracted = s_data.get("extracted_entities")
        if isinstance(extracted, dict):
            entities = extracted
        else:
            entities = {
                "order_id": s_data.get("order_id"),
                "product_name": s_data.get("product_name"),
                "email": s_data.get("email")
            }
    except SentimentClassificationError as e:
        # Agent is up but answered badly: escalate, but don't trip the availability breaker.
        logger.error(f"Sentiment agent returned an unusable classification: {e}")
        sentiment_failed = True
    except Exception as e:
        logger.error(f"Error executing sentiment agent: {e}")
        rag_circuit_breaker.record_failure(503)
        sentiment_failed = True
    is_urgent = sentiment_failed or sentiment == SENTIMENT_URGENT

    calming_ack = None
    if is_urgent:
        if sentiment_failed:
            calming_ack = (
                "Your request has been forwarded directly to our human support queue service, and a specialist will reach out shortly."
            )
        else:
            order_id = entities.get("order_id", "")
            order_str = f" for Order #{order_id}" if order_id else ""
            calming_ack = (
                f"We hear your frustration regarding your request{order_str} and sincerely apologize for the inconvenience. "
                f"Your issue has been prioritized as URGENT and routed directly to our Senior Human Support Queue. "
                f"A dedicated support specialist will review your ticket state in Cloud SQL and reach out to you immediately."
            )

    # Step 2: One transaction: create or update the ticket, store the customer message and,
    # for urgent turns, the SYSTEM ack. All of it is written, or none of it.
    escalated_meanwhile = False
    with tracer.start_as_current_span("cloud_sql_sync_ticket") as span:
        span.set_attribute("ticket.id", ticket_id)
        span.set_attribute("customer.id", caller)
        span.set_attribute("db.system", "postgresql")

        with db.begin():
            db_ticket = (
                get_owned_ticket(db, ticket_id, caller, for_update=True)
                if existing_status is not None else None
            )
            if db_ticket is not None and db_ticket.current_status == "RESOLVED":
                # Resolved in another tab while the sentiment agent was running.
                raise HTTPException(status_code=400, detail=TICKET_RESOLVED_DURING_REQUEST_DETAIL)

            if db_ticket is not None and db_ticket.current_status == "ESCALATED" and not is_urgent:
                # Escalated by a concurrent request: this turn belongs to the human now.
                escalated_meanwhile = True
            elif db_ticket is not None:
                if is_urgent:
                    db_ticket.sentiment = SENTIMENT_URGENT if not sentiment_failed else "UNAVAILABLE"
                    db_ticket.current_status = "ESCALATED"
                elif db_ticket.current_status == "NEW":
                    db_ticket.sentiment = sentiment
                    db_ticket.current_status = "IN_PROGRESS"
            else:
                db_ticket = Ticket(
                    id=ticket_id,
                    customer_id=caller,
                    current_status="ESCALATED" if is_urgent else "IN_PROGRESS",
                    sentiment=sentiment if not sentiment_failed else "UNAVAILABLE"
                )
                db.add(db_ticket)

            db.add(Message(ticket_id=ticket_id, sender="CUSTOMER", message_text=request.message))
            if is_urgent:
                db.add(Message(ticket_id=ticket_id, sender="SYSTEM", message_text=calming_ack))
            ticket_status = db_ticket.current_status
            ticket_sentiment = db_ticket.sentiment
        span.set_attribute("ticket.status", ticket_status)

    if escalated_meanwhile:
        return escalated_followup_response(ticket_id, ticket_sentiment)

    # Step 3: Urgent / Frustrated Routing or Sentiment Unavailable Fallback
    if is_urgent:
        return SupportQueryResponse(
            ticket_id=ticket_id,
            ticket_status=ticket_status,
            answer=calming_ack,
            sentiment=sentiment if not sentiment_failed else "UNAVAILABLE",
            routed_to="Human Escalation Queue",
            extracted_entities=entities,
            citations=["Assigned to Human Support Specialist Queue"],
            grounded=False,
            session_id=ticket_id,
            agent_service_version="v2.5-human-escalation"
        )

    # Step 4: Check Circuit Breaker State for FAQ Agent
    # CLOSED: normal call. HALF_OPEN: this request is the single recovery probe.
    # None: OPEN, or HALF_OPEN with a probe already in flight, so take the fallback.
    admitted_as = rag_circuit_breaker.try_acquire()
    allowed = admitted_as is not None
    is_probe = admitted_as == CircuitState.HALF_OPEN

    with tracer.start_as_current_span("circuit_breaker_check") as cb_span:
        cb_span.set_attribute("circuit_breaker.state", rag_circuit_breaker.state.value)
        cb_span.set_attribute("circuit_breaker.failure_count", rag_circuit_breaker.failure_count)
        cb_span.set_attribute("circuit_breaker.allowed", allowed)
        cb_span.set_attribute("circuit_breaker.probe", is_probe)

    if not allowed:
        logger.warning("Circuit breaker OPEN. Bypassing Agent Platform / RAG and routing directly to Human Queue Outage Fallback.")
        fallback_ans = "We are currently experiencing high volume or downstream service delays. Your request has been forwarded to our human queue service."
        
        with tracer.start_as_current_span("cloud_sql_record_fallback") as fb_span:
            fb_span.set_attribute("ticket.id", ticket_id)
            fb_span.set_attribute("db.system", "postgresql")
            ticket_status = record_turn(db, ticket_id, caller, "SYSTEM", fallback_ans, escalate=True)

        return SupportQueryResponse(
            ticket_id=ticket_id,
            ticket_status=ticket_status,
            answer=fallback_ans,
            sentiment=sentiment,
            routed_to="Mock Human Queue Service (Circuit Breaker OPEN Fallback)",
            extracted_entities=entities,
            citations=["Circuit Breaker OPEN ➔ Outage Fallback"],
            grounded=False,
            session_id=ticket_id,
            agent_service_version="v2.5-circuit-breaker-fallback"
        )

    # Step 5: Call FAQ Assistant Agent. The breaker records ONE outcome per customer
    # question (TDD: 3 consecutive failed questions open it), however many attempts it took.
    if is_probe:
        logger.info("Circuit breaker in HALF_OPEN state: executing single probe test query...")
    try:
        faq_data, failure_status = await query_faq_with_retries(
            request.message, request.session_id, agent_user_id(caller),
            max_attempts=1 if is_probe else FAQ_MAX_ATTEMPTS,
        )
        if faq_data:
            rag_circuit_breaker.record_success()
        else:
            rag_circuit_breaker.record_failure(failure_status or 503)
    finally:
        if is_probe:
            # No-op if record_* already settled the probe; frees the slot after a 4xx.
            rag_circuit_breaker.release_probe()

    # If FAQ Agent failed (all attempts failed or probe failed)
    if not faq_data:
        fallback_ans = "We are currently experiencing high volume or downstream service delays. Your request has been forwarded to our human queue service."
        with tracer.start_as_current_span("cloud_sql_record_fallback") as fb_span:
            fb_span.set_attribute("ticket.id", ticket_id)
            fb_span.set_attribute("db.system", "postgresql")
            ticket_status = record_turn(db, ticket_id, caller, "SYSTEM", fallback_ans, escalate=True)

        return SupportQueryResponse(
            ticket_id=ticket_id,
            ticket_status=ticket_status,
            answer=fallback_ans,
            sentiment=sentiment,
            routed_to="Mock Human Queue Service (Circuit Breaker OPEN Fallback)",
            extracted_entities=entities,
            citations=["Circuit Breaker Failure ➔ Outage Fallback"],
            grounded=False,
            session_id=ticket_id,
            agent_service_version="v2.5-circuit-breaker-fallback"
        )

    answer_text = faq_data.get("answer", "No answer generated.")
    citations = faq_data.get("citations", [])
    grounded = faq_data.get("grounded", True)
    escalate = bool(faq_data.get("escalate", False))

    # Parse nested JSON or Markdown if returned in answer string
    if isinstance(answer_text, str):
        raw_str = answer_text.strip()
        if raw_str.startswith("```json"):
            raw_str = raw_str.split("```json")[-1].split("```")[0].strip()
        elif raw_str.startswith("```"):
            raw_str = raw_str.split("```")[1].strip()
        
        if raw_str.startswith("{") and raw_str.endswith("}"):
            try:
                parsed = json.loads(raw_str, strict=False)
                if isinstance(parsed, dict) and "answer" in parsed:
                    answer_text = parsed["answer"]
                    if "citations" in parsed and isinstance(parsed["citations"], list):
                        for c in parsed["citations"]:
                            if str(c) not in citations:
                                citations.append(str(c))
                    if "grounded" in parsed:
                        grounded = bool(parsed["grounded"])
                    if "escalate" in parsed:
                        escalate = bool(parsed["escalate"])
            except Exception:
                match = re.search(r'"answer":\s*"(.*?)"\s*,\s*"(?:citations|grounded)"', raw_str, re.DOTALL)
                if match:
                    answer_text = match.group(1).replace("\\n", "\n").replace('\\"', '"')

    # Defense in depth: never show doc IDs / retrieval-backend references to customers.
    # Doc IDs remain available in `citations` for debugging.
    answer_text = sanitize_answer(answer_text)

    with tracer.start_as_current_span("cloud_sql_record_agent_response") as db_span:
        db_span.set_attribute("ticket.id", ticket_id)
        db_span.set_attribute("db.system", "postgresql")
        db_span.set_attribute("faq.grounded", grounded)
        db_span.set_attribute("faq.citations_count", len(citations))
        # escalate=True: the agent couldn't resolve it (no documented answer, action request, tool error).
        ticket_status = record_turn(db, ticket_id, caller, "AGENT", answer_text, escalate=escalate)

    # No placeholder citation: clarifying questions legitimately use no tools
    formatted_citations = format_citations(citations)

    return SupportQueryResponse(
        ticket_id=ticket_id,
        ticket_status=ticket_status,
        answer=answer_text,
        sentiment=sentiment,
        routed_to="Automated RAG Assistant ➔ Human Escalation Queue" if escalate else "Automated RAG Assistant",
        extracted_entities=entities,
        citations=formatted_citations,
        grounded=grounded,
        session_id=ticket_id,
        agent_service_version="v2.5-faq-assistant"
    )

@app.post("/api/tickets/create")
async def create_ticket(request: CreateTicketRequest | None = None, db: Session = Depends(get_db), caller: str = Depends(get_caller)):
    """Creates a new ticket with status 'NEW' owned by the caller. The ID is generated server-side."""
    ticket_id = new_ticket_id()
    with db.begin():
        db.add(Ticket(
            id=ticket_id,
            customer_id=caller,
            current_status="NEW",
            sentiment="NEUTRAL_FACTUAL"
        ))
    logger.info(f"Created new ticket in Cloud SQL: id={ticket_id}, status=NEW")

    return {
        "status": "success",
        "ticket_id": ticket_id,
        "ticket_status": "NEW",
        "customer_id": caller
    }

@app.post("/api/tickets/{ticket_id}/resolve")
async def resolve_ticket(ticket_id: str, db: Session = Depends(get_db), caller: str = Depends(get_caller)):
    with db.begin():
        db_ticket = get_owned_ticket(db, ticket_id, caller, for_update=True)
        db_ticket.current_status = "RESOLVED"
        db.add(Message(
            ticket_id=ticket_id,
            sender="SYSTEM",
            message_text="Ticket has been marked as RESOLVED by the customer."
        ))

    # Resolved tickets can't receive new messages, so the agent's memory is no longer needed.
    # Runs after the commit, so no transaction is held during the Agent Runtime call.
    await delete_faq_session(ticket_id, agent_user_id(caller))

    return {
        "status": "success",
        "ticket_id": ticket_id,
        "current_status": "RESOLVED",
        "message": f"Ticket {ticket_id} has been marked as RESOLVED."
    }

@app.get("/api/tickets/{ticket_id}")
async def get_ticket_details(ticket_id: str, db: Session = Depends(get_db), caller: str = Depends(get_caller)):
    ticket = get_owned_ticket(db, ticket_id, caller)
    
    messages = db.query(Message).filter(Message.ticket_id == ticket.id).all()
    return {
        "ticket": {
            "id": ticket.id,
            "customer_id": ticket.customer_id,
            "current_status": ticket.current_status,
            "sentiment": ticket.sentiment,
            "created_at": ticket.created_at
        },
        "messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "message_text": m.message_text,
                "created_at": m.created_at
            }
            for m in messages
        ]
    }

@app.get("/", response_class=HTMLResponse)
async def get_index():
    return """<!DOCTYPE html>
<html>
<head><title>FDE Support Decoupled Backend API</title></head>
<body><h1>FDE Support Decoupled Backend Active with Cloud SQL Persistence</h1></body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
