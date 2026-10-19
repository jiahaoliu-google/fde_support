import os
import re
import json
import uuid
import hashlib
import logging
import asyncio
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

# Ensure database tables exist on module import / startup
try:
    init_db()
except Exception as e:
    logger.warning(f"Initial DB check warning: {e}")

# Configurable Agent Runtime Microservice Endpoints on Vertex AI
SENTIMENT_AGENT_URL = settings.SENTIMENT_AGENT_URL
FAQ_AGENT_URL = settings.FAQ_AGENT_URL

# Instantiate CircuitBreaker from settings
rag_circuit_breaker = CircuitBreaker(
    failure_threshold=settings.CIRCUIT_FAILURE_THRESHOLD,
    recovery_timeout_seconds=settings.CIRCUIT_RECOVERY_TIMEOUT
)

def get_gcp_auth_headers() -> dict:
    """Retrieves GCP OAuth access token for calling Reasoning Engine endpoints."""
    try:
        import google.auth
        import google.auth.transport.requests
        credentials, _ = google.auth.default()
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        return {"Authorization": f"Bearer {credentials.token}"}
    except Exception as e:
        logger.warning(f"GCP Auth token acquisition failed: {e}")
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
                    span.set_attribute("sentiment.classified", result.get("sentiment", "UNKNOWN"))
                    return result
                except Exception:
                    span.set_attribute("sentiment.classified", "NEUTRAL_FACTUAL")
                    return {"sentiment": "NEUTRAL_FACTUAL"}
                    
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

def get_owned_ticket(db: Session, ticket_id: str, caller: str) -> Ticket:
    """Fetches a ticket only if it belongs to the caller. 404 (not 403) avoids revealing existence."""
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id, Ticket.customer_id == caller).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


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

def append_to_escalated_ticket(db: Session, ticket: Ticket, message: str) -> "SupportQueryResponse":
    """Records a customer follow-up for the human handling an ESCALATED ticket. No LLM calls."""
    with tracer.start_as_current_span("escalated_ticket_followup") as span:
        span.set_attribute("ticket.id", ticket.id)
        db.add(Message(ticket_id=ticket.id, sender="CUSTOMER", message_text=message))
        db.commit()
    # The ack is returned to the UI but not stored, so the human's transcript holds only
    # real customer turns.
    return SupportQueryResponse(
        ticket_id=ticket.id,
        ticket_status="ESCALATED",
        answer=ESCALATED_FOLLOWUP_ACK,
        sentiment=ticket.sentiment or "UNAVAILABLE",
        routed_to="Human Escalation Queue",
        extracted_entities={},
        citations=["Assigned to Human Support Specialist Queue"],
        grounded=False,
        session_id=ticket.id,
        agent_service_version="v2.5-human-escalation",
    )


@app.post("/api/chat", response_model=SupportQueryResponse)
async def process_support_query(request: SupportQueryRequest, db: Session = Depends(get_db), caller: str = Depends(get_caller)):
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # Ownership check BEFORE any LLM call: a foreign/unknown ticket ID is rejected,
    # so callers can never append to (or read agent memory of) another user's ticket.
    existing_ticket = None
    if request.session_id:
        existing_ticket = get_owned_ticket(db, request.session_id, caller)
        if existing_ticket.current_status == "RESOLVED":
            raise HTTPException(
                status_code=400,
                detail="This ticket is marked as RESOLVED. Please click 'Start New Ticket' to submit new inquiries."
            )
    else:
        request.session_id = new_ticket_id()
    request.customer_id = caller

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
    if existing_ticket is not None and existing_ticket.current_status == "ESCALATED":
        return append_to_escalated_ticket(db, existing_ticket, request.message)

    # Step 1: Call Sentiment Agent on Agent Runtime over HTTP
    sentiment = "NEUTRAL_FACTUAL"
    entities = {}
    sentiment_failed = False
    
    try:
        s_data = await call_sentiment_agent(request.message)
        sentiment = s_data.get("sentiment", "NEUTRAL_FACTUAL")
        
        extracted = s_data.get("extracted_entities")
        if isinstance(extracted, dict):
            entities = extracted
        else:
            entities = {
                "order_id": s_data.get("order_id"),
                "product_name": s_data.get("product_name"),
                "email": s_data.get("email")
            }
    except Exception as e:
        logger.error(f"Error executing sentiment agent: {e}")
        rag_circuit_breaker.record_failure(503)
        sentiment_failed = True

    # Step 2: Look up or create Ticket in Cloud SQL/DB using session_id as ticket.id
    with tracer.start_as_current_span("cloud_sql_sync_ticket") as span:
        span.set_attribute("ticket.id", request.session_id)
        span.set_attribute("customer.id", request.customer_id)
        span.set_attribute("db.system", "postgresql")

        db_ticket = db.query(Ticket).filter(Ticket.id == request.session_id, Ticket.customer_id == caller).first()
        
        if db_ticket:
            if db_ticket.current_status == "RESOLVED":
                raise HTTPException(
                    status_code=400,
                    detail="This ticket is marked as RESOLVED. Please click 'Start New Ticket' to submit new inquiries."
                )
            if sentiment in ["URGENT_FRUSTRATED", "ANGRY"] or sentiment_failed:
                db_ticket.sentiment = "URGENT_FRUSTRATED" if not sentiment_failed else "UNAVAILABLE"
                db_ticket.current_status = "ESCALATED"
            elif db_ticket.current_status == "NEW":
                db_ticket.sentiment = sentiment
                db_ticket.current_status = "IN_PROGRESS"
        else:
            initial_status = "ESCALATED" if (sentiment in ["URGENT_FRUSTRATED", "ANGRY"] or sentiment_failed) else "IN_PROGRESS"
            db_ticket = Ticket(
                id=request.session_id,
                customer_id=request.customer_id,
                current_status=initial_status,
                sentiment=sentiment if not sentiment_failed else "UNAVAILABLE"
            )
            db.add(db_ticket)

        db.commit()
        db.refresh(db_ticket)

        # Record Customer Message
        cust_msg = Message(
            ticket_id=db_ticket.id,
            sender="CUSTOMER",
            message_text=request.message
        )
        db.add(cust_msg)
        db.commit()
        span.set_attribute("ticket.status", db_ticket.current_status)

    # Step 3: Urgent / Frustrated Routing or Sentiment Unavailable Fallback
    if sentiment_failed or sentiment in ["URGENT_FRUSTRATED", "ANGRY"]:
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
        
        db_ticket.current_status = "ESCALATED"
        sys_msg = Message(
            ticket_id=db_ticket.id,
            sender="SYSTEM",
            message_text=calming_ack
        )
        db.add(sys_msg)
        db.commit()

        return SupportQueryResponse(
            ticket_id=db_ticket.id,
            ticket_status=db_ticket.current_status,
            answer=calming_ack,
            sentiment=sentiment if not sentiment_failed else "UNAVAILABLE",
            routed_to="Human Escalation Queue",
            extracted_entities=entities,
            citations=["Assigned to Human Support Specialist Queue"],
            grounded=False,
            session_id=request.session_id,
            agent_service_version="v2.5-human-escalation"
        )

    # Step 4: Check Circuit Breaker State for FAQ Agent
    initial_cb_state = rag_circuit_breaker.state
    allowed = rag_circuit_breaker.allow_request()

    with tracer.start_as_current_span("circuit_breaker_check") as cb_span:
        cb_span.set_attribute("circuit_breaker.state", initial_cb_state.value)
        cb_span.set_attribute("circuit_breaker.failure_count", rag_circuit_breaker.failure_count)
        cb_span.set_attribute("circuit_breaker.allowed", allowed)

    if not allowed:
        logger.warning("Circuit breaker OPEN. Bypassing Agent Platform / RAG and routing directly to Human Queue Outage Fallback.")
        fallback_ans = "We are currently experiencing high volume or downstream service delays. Your request has been forwarded to our human queue service."
        
        with tracer.start_as_current_span("cloud_sql_record_fallback") as fb_span:
            fb_span.set_attribute("ticket.id", db_ticket.id)
            fb_span.set_attribute("db.system", "postgresql")
            db_ticket.current_status = "ESCALATED"
            fallback_msg = Message(
                ticket_id=db_ticket.id,
                sender="SYSTEM",
                message_text=fallback_ans
            )
            db.add(fallback_msg)
            db.commit()

        return SupportQueryResponse(
            ticket_id=db_ticket.id,
            ticket_status=db_ticket.current_status,
            answer=fallback_ans,
            sentiment=sentiment,
            routed_to="Mock Human Queue Service (Circuit Breaker OPEN Fallback)",
            extracted_entities=entities,
            citations=["Circuit Breaker OPEN ➔ Outage Fallback"],
            grounded=False,
            session_id=request.session_id,
            agent_service_version="v2.5-circuit-breaker-fallback"
        )

    # Step 5: Call FAQ Assistant Agent based on Circuit Breaker state (CLOSED vs HALF_OPEN)
    faq_data = None
    
    if initial_cb_state == CircuitState.HALF_OPEN or rag_circuit_breaker.state == CircuitState.HALF_OPEN:
        # HALF_OPEN state: Single probe test query (1 attempt only)
        logger.info("Circuit breaker in HALF_OPEN state: executing single probe test query...")
        try:
            faq_data = await call_faq_agent(request.message, request.session_id, agent_user_id(caller))
            rag_circuit_breaker.record_success()
        except Exception as e:
            logger.warning(f"FAQ agent probe in HALF_OPEN state failed: {e}")
            status_code = getattr(e, "status_code", 503)
            if status_code not in (400, 422):
                rag_circuit_breaker.record_failure(status_code)
    else:
        # CLOSED state: Real user query, try up to 3 times with 5s delay between attempts
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                faq_data = await call_faq_agent(request.message, request.session_id, agent_user_id(caller))
                rag_circuit_breaker.record_success()
                break
            except Exception as e:
                logger.warning(f"FAQ agent attempt {attempt}/{max_attempts} failed: {e}")
                status_code = getattr(e, "status_code", 503)
                if status_code not in (400, 422):
                    rag_circuit_breaker.record_failure(status_code)
                if attempt < max_attempts:
                    await asyncio.sleep(5)

    # If FAQ Agent failed (all attempts failed or probe failed)
    if not faq_data:
        fallback_ans = "We are currently experiencing high volume or downstream service delays. Your request has been forwarded to our human queue service."
        with tracer.start_as_current_span("cloud_sql_record_fallback") as fb_span:
            fb_span.set_attribute("ticket.id", db_ticket.id)
            fb_span.set_attribute("db.system", "postgresql")
            db_ticket.current_status = "ESCALATED"
            fallback_msg = Message(
                ticket_id=db_ticket.id,
                sender="SYSTEM",
                message_text=fallback_ans
            )
            db.add(fallback_msg)
            db.commit()

        return SupportQueryResponse(
            ticket_id=db_ticket.id,
            ticket_status=db_ticket.current_status,
            answer=fallback_ans,
            sentiment=sentiment,
            routed_to="Mock Human Queue Service (Circuit Breaker OPEN Fallback)",
            extracted_entities=entities,
            citations=["Circuit Breaker Failure ➔ Outage Fallback"],
            grounded=False,
            session_id=request.session_id,
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
        db_span.set_attribute("ticket.id", db_ticket.id)
        db_span.set_attribute("db.system", "postgresql")
        db_span.set_attribute("faq.grounded", grounded)
        db_span.set_attribute("faq.citations_count", len(citations))

        if escalate:
            # Agent could not resolve (no documented answer, action request, tool error)
            db_ticket.current_status = "ESCALATED"
        elif db_ticket.current_status == "NEW":
            db_ticket.current_status = "IN_PROGRESS"

        agent_msg = Message(
            ticket_id=db_ticket.id,
            sender="AGENT",
            message_text=answer_text
        )
        db.add(agent_msg)
        db.commit()

    # No placeholder citation: clarifying questions legitimately use no tools
    formatted_citations = format_citations(citations)

    return SupportQueryResponse(
        ticket_id=db_ticket.id,
        ticket_status=db_ticket.current_status,
        answer=answer_text,
        sentiment=sentiment,
        routed_to="Automated RAG Assistant ➔ Human Escalation Queue" if escalate else "Automated RAG Assistant",
        extracted_entities=entities,
        citations=formatted_citations,
        grounded=grounded,
        session_id=request.session_id,
        agent_service_version="v2.5-faq-assistant"
    )

@app.post("/api/tickets/create")
async def create_ticket(request: CreateTicketRequest | None = None, db: Session = Depends(get_db), caller: str = Depends(get_caller)):
    """Creates a new ticket with status 'NEW' owned by the caller. The ID is generated server-side."""
    db_ticket = Ticket(
        id=new_ticket_id(),
        customer_id=caller,
        current_status="NEW",
        sentiment="NEUTRAL_FACTUAL"
    )
    db.add(db_ticket)
    db.commit()
    db.refresh(db_ticket)
    logger.info(f"Created new ticket in Cloud SQL: id={db_ticket.id}, status=NEW")
    
    return {
        "status": "success",
        "ticket_id": db_ticket.id,
        "ticket_status": db_ticket.current_status,
        "customer_id": db_ticket.customer_id
    }

@app.post("/api/tickets/{ticket_id}/resolve")
async def resolve_ticket(ticket_id: str, db: Session = Depends(get_db), caller: str = Depends(get_caller)):
    db_ticket = get_owned_ticket(db, ticket_id, caller)
    db_ticket.current_status = "RESOLVED"

    sys_msg = Message(
        ticket_id=db_ticket.id,
        sender="SYSTEM",
        message_text="Ticket has been marked as RESOLVED by the customer."
    )
    db.add(sys_msg)
    db.commit()

    # Resolved tickets can't receive new messages, so the agent's memory is no longer needed.
    await delete_faq_session(db_ticket.id, agent_user_id(caller))

    return {
        "status": "success",
        "ticket_id": db_ticket.id,
        "current_status": "RESOLVED",
        "message": f"Ticket {db_ticket.id} has been marked as RESOLVED."
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
