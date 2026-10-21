"""Support-chat pipeline behind POST /api/chat.

One customer turn: ownership check -> Model Armor screening + PII redaction -> sentiment ->
persist -> route (human queue, outage fallback, or FAQ agent) -> persist the reply.

Agent calls and the circuit breaker are always reached as `agent_runtime.<name>`
so test patches on backend.clients.agent_runtime apply here.
"""
import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from agents.faq_assistant.answer_sanitizer import sanitize_answer
from backend.api.deps import agent_user_id
from backend.api.schemas import SupportQueryResponse
from backend.clients import agent_runtime, stream_parsing
from backend.core.circuit_breaker import CircuitState
from backend.core.telemetry import tracer
from backend.db.models import Message, Ticket
from backend.security.model_armor import ScreeningResult, model_armor
from backend.services.tickets import (
    TICKET_EXPIRED_DETAIL,
    TICKET_RESOLVED_DETAIL,
    TICKET_RESOLVED_DURING_REQUEST_DETAIL,
    get_owned_ticket,
    is_ttl_expired,
    new_ticket_id,
    record_turn,
)

logger = logging.getLogger("backend")

# ---------------------------------------------------------------------------
# Customer-facing text and routing labels
# ---------------------------------------------------------------------------
ESCALATED_FOLLOWUP_ACK = (
    "Your ticket is with our human support team. We've added your message to it, "
    "and a specialist will reply here."
)
SENTIMENT_UNAVAILABLE_ACK = (
    "Your request has been forwarded directly to our human support queue service, and a specialist will reach out shortly."
)
OUTAGE_FALLBACK_ANSWER = (
    "We are currently experiencing high volume or downstream service delays. "
    "Your request has been forwarded to our human queue service."
)

ROUTE_HUMAN_QUEUE = "Human Escalation Queue"
ROUTE_OUTAGE_FALLBACK = "Mock Human Queue Service (Circuit Breaker OPEN Fallback)"
ROUTE_FAQ = "Automated RAG Assistant"
ROUTE_FAQ_ESCALATED = "Automated RAG Assistant ➔ Human Escalation Queue"

CITATION_HUMAN_QUEUE = "Assigned to Human Support Specialist Queue"
CITATION_BREAKER_OPEN = "Circuit Breaker OPEN ➔ Outage Fallback"
CITATION_BREAKER_FAILURE = "Circuit Breaker Failure ➔ Outage Fallback"

VERSION_HUMAN_ESCALATION = "v2.5-human-escalation"
VERSION_BREAKER_FALLBACK = "v2.5-circuit-breaker-fallback"
VERSION_FAQ = "v2.5-faq-assistant"

SENTIMENT_UNAVAILABLE = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------
def _human_queue_response(
    ticket_id: str,
    ticket_status: str,
    answer: str,
    sentiment: str,
    *,
    entities: dict | None = None,
    routed_to: str = ROUTE_HUMAN_QUEUE,
    citation: str = CITATION_HUMAN_QUEUE,
    version: str = VERSION_HUMAN_ESCALATION,
) -> SupportQueryResponse:
    """Any reply that didn't come from the FAQ agent: escalation, follow-up ack, outage fallback."""
    return SupportQueryResponse(
        ticket_id=ticket_id,
        ticket_status=ticket_status,
        answer=answer,
        sentiment=sentiment,
        routed_to=routed_to,
        extracted_entities=entities or {},
        citations=[citation],
        grounded=False,
        session_id=ticket_id,
        agent_service_version=version,
    )


def escalated_followup_response(ticket_id: str, sentiment: str | None) -> SupportQueryResponse:
    # The ack is returned to the UI but not stored, so the human's transcript holds only
    # real customer turns.
    return _human_queue_response(
        ticket_id, "ESCALATED", ESCALATED_FOLLOWUP_ACK, sentiment or SENTIMENT_UNAVAILABLE,
    )


def append_to_escalated_ticket(db: Session, ticket_id: str, caller: str, message: str) -> SupportQueryResponse:
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


def _outage_fallback(db: Session, ticket_id: str, caller: str, sentiment: str, entities: dict,
                     citation: str) -> SupportQueryResponse:
    """FAQ agent unavailable (breaker open or every attempt failed): escalate to the human queue."""
    with tracer.start_as_current_span("cloud_sql_record_fallback") as fb_span:
        fb_span.set_attribute("ticket.id", ticket_id)
        fb_span.set_attribute("db.system", "postgresql")
        ticket_status = record_turn(db, ticket_id, caller, "SYSTEM", OUTAGE_FALLBACK_ANSWER, escalate=True)

    return _human_queue_response(
        ticket_id, ticket_status, OUTAGE_FALLBACK_ANSWER, sentiment,
        entities=entities, routed_to=ROUTE_OUTAGE_FALLBACK, citation=citation,
        version=VERSION_BREAKER_FALLBACK,
    )


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------
def _screen(message: str) -> ScreeningResult:
    """Model Armor: redacts PII and screens for prompt injection / harmful content before any
    LLM call. Only filter names and states go into the span, never the text."""
    with tracer.start_as_current_span("model_armor_redact") as span:
        result = model_armor.screen(message)
        redacted = (result.text != message)
        span.set_attribute("model_armor.redacted", redacted)
        span.set_attribute("model_armor.path", result.path)
        span.set_attribute("model_armor.threat_screened", result.threat_screened)
        span.set_attribute("model_armor.blocked", result.blocked)
        span.set_attribute("model_armor.urgent", result.urgent)
        span.set_attribute("model_armor.findings", ",".join(result.findings))
        if redacted:
            logger.info("Model Armor successfully redacted sensitive customer PII before LLM dispatch.")
    return result


async def _classify_sentiment(message: str) -> tuple[str, dict, bool]:
    """Calls the Sentiment agent. Returns (sentiment, entities, failed).

    Fails closed: if no valid label comes back, failed=True and the ticket goes to a human.
    """
    sentiment = agent_runtime.SENTIMENT_NEUTRAL
    entities = {}
    try:
        s_data = await agent_runtime.call_sentiment_agent(message)
        sentiment = s_data["sentiment"]  # validated against agent_runtime.SENTIMENT_LABELS

        extracted = s_data.get("extracted_entities")
        if isinstance(extracted, dict):
            entities = extracted
        else:
            entities = {
                "order_id": s_data.get("order_id"),
                "product_name": s_data.get("product_name"),
                "email": s_data.get("email")
            }
    except agent_runtime.SentimentClassificationError as e:
        # Agent is up but answered badly: escalate, but don't trip the availability breaker.
        logger.error(f"Sentiment agent returned an unusable classification: {e}")
        return sentiment, entities, True
    except Exception as e:
        logger.error(f"Error executing sentiment agent: {e}")
        agent_runtime.rag_circuit_breaker.record_failure(503)
        return sentiment, entities, True
    return sentiment, entities, False


def _urgent_ack(entities: dict, sentiment_failed: bool) -> str:
    if sentiment_failed:
        return SENTIMENT_UNAVAILABLE_ACK
    order_id = entities.get("order_id", "")
    order_str = f" for Order #{order_id}" if order_id else ""
    return (
        f"We hear your frustration regarding your request{order_str} and sincerely apologize for the inconvenience. "
        f"Your issue has been prioritized as URGENT and routed directly to our Senior Human Support Queue. "
        f"A dedicated support specialist will review your ticket state in Cloud SQL and reach out to you immediately."
    )


def _sync_ticket(db: Session, ticket_id: str, caller: str, *, existing_status: str | None, message: str,
                 sentiment: str, sentiment_failed: bool, calming_ack: str | None) -> tuple[str, str, bool]:
    """One transaction: create or update the ticket, store the customer message and, for urgent
    turns, the SYSTEM ack. All of it is written, or none of it.

    Returns (ticket_status, ticket_sentiment, escalated_meanwhile).
    """
    is_urgent = calming_ack is not None
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
                    db_ticket.sentiment = agent_runtime.SENTIMENT_URGENT if not sentiment_failed else SENTIMENT_UNAVAILABLE
                    db_ticket.current_status = "ESCALATED"
                elif db_ticket.current_status == "NEW":
                    db_ticket.sentiment = sentiment
                    db_ticket.current_status = "IN_PROGRESS"
            else:
                db_ticket = Ticket(
                    id=ticket_id,
                    customer_id=caller,
                    current_status="ESCALATED" if is_urgent else "IN_PROGRESS",
                    sentiment=sentiment if not sentiment_failed else SENTIMENT_UNAVAILABLE
                )
                db.add(db_ticket)

            db.add(Message(ticket_id=ticket_id, sender="CUSTOMER", message_text=message))
            if is_urgent:
                db.add(Message(ticket_id=ticket_id, sender="SYSTEM", message_text=calming_ack))
            ticket_status = db_ticket.current_status
            ticket_sentiment = db_ticket.sentiment
        span.set_attribute("ticket.status", ticket_status)
    return ticket_status, ticket_sentiment, escalated_meanwhile


def _admit_through_breaker() -> CircuitState | None:
    """CLOSED: normal call. HALF_OPEN: this request is the single recovery probe.
    None: OPEN, or HALF_OPEN with a probe already in flight, so take the fallback."""
    breaker = agent_runtime.rag_circuit_breaker
    admitted_as = breaker.try_acquire()
    with tracer.start_as_current_span("circuit_breaker_check") as cb_span:
        cb_span.set_attribute("circuit_breaker.state", breaker.state.value)
        cb_span.set_attribute("circuit_breaker.failure_count", breaker.failure_count)
        cb_span.set_attribute("circuit_breaker.allowed", admitted_as is not None)
        cb_span.set_attribute("circuit_breaker.probe", admitted_as == CircuitState.HALF_OPEN)
    return admitted_as


async def _ask_faq_agent(message: str, ticket_id: str, caller: str, *, is_probe: bool) -> dict | None:
    """Calls the FAQ agent. The breaker records ONE outcome per customer question
    (TDD: 3 consecutive failed questions open it), however many attempts it took."""
    breaker = agent_runtime.rag_circuit_breaker
    if is_probe:
        logger.info("Circuit breaker in HALF_OPEN state: executing single probe test query...")
    try:
        faq_data, failure_status = await agent_runtime.query_faq_with_retries(
            message, ticket_id, agent_user_id(caller),
            max_attempts=1 if is_probe else agent_runtime.FAQ_MAX_ATTEMPTS,
        )
        if faq_data:
            breaker.record_success()
        else:
            breaker.record_failure(failure_status or 503)
    finally:
        if is_probe:
            # No-op if record_* already settled the probe; frees the slot after a 4xx.
            breaker.release_probe()
    return faq_data


def _faq_answer_response(db: Session, ticket_id: str, caller: str, sentiment: str, entities: dict,
                         faq_data: dict) -> SupportQueryResponse:
    answer_text, citations, grounded, escalate = stream_parsing.unwrap_faq_answer(faq_data)

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

    return SupportQueryResponse(
        ticket_id=ticket_id,
        ticket_status=ticket_status,
        answer=answer_text,
        sentiment=sentiment,
        routed_to=ROUTE_FAQ_ESCALATED if escalate else ROUTE_FAQ,
        extracted_entities=entities,
        # No placeholder citation: clarifying questions legitimately use no tools
        citations=stream_parsing.format_citations(citations),
        grounded=grounded,
        session_id=ticket_id,
        agent_service_version=VERSION_FAQ,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def handle_support_query(db: Session, caller: str, message: str,
                               session_id: str | None) -> SupportQueryResponse:
    """Processes one customer message on a new (session_id=None) or existing ticket."""
    # Ownership check BEFORE any LLM call: a foreign/unknown ticket ID is rejected,
    # so callers can never append to (or read agent memory of) another user's ticket.
    # Read-only transaction: it ends here, before any agent call.
    existing_status = None
    if session_id:
        with db.begin():
            existing_ticket = get_owned_ticket(db, session_id, caller)
            existing_status = existing_ticket.current_status
            existing_created_at = existing_ticket.created_at
        if existing_status == "RESOLVED":
            raise HTTPException(status_code=400, detail=TICKET_RESOLVED_DETAIL)
        if is_ttl_expired(existing_created_at):
            raise HTTPException(status_code=400, detail=TICKET_EXPIRED_DETAIL)
        ticket_id = session_id
    else:
        ticket_id = new_ticket_id()

    # Step 0: Model Armor screening. PII is redacted before anything is sent to Gemini
    # agents or stored.
    screening = _screen(message)
    message = screening.text

    # Escalated tickets belong to the human queue (scoping doc): no sentiment or FAQ call.
    # This is also what keeps the FAQ agent's memory consistent with the transcript: every
    # turn it never saw ends with the ticket ESCALATED, and it is never asked again.
    if existing_status == "ESCALATED":
        return append_to_escalated_ticket(db, ticket_id, caller, message)

    # Step 1: sentiment. A failed classification is treated as urgent (fail closed).
    if screening.blocked:
        # Prompt injection / jailbreak / malicious URI: no LLM sees the message. A human
        # reviews it. The reply is the same as when sentiment is unavailable, so the sender
        # can't tell that screening flagged it.
        logger.warning("Model Armor blocked a customer message (%s); routing ticket %s to the "
                       "human queue without agent calls.", ",".join(screening.findings), ticket_id)
        sentiment, entities, sentiment_failed = SENTIMENT_UNAVAILABLE, {}, True
    elif screening.urgent:
        # Responsible AI filter match (abusive / harmful content): a human handles it.
        sentiment, entities, sentiment_failed = agent_runtime.SENTIMENT_URGENT, {}, False
    else:
        sentiment, entities, sentiment_failed = await _classify_sentiment(message)
    is_urgent = sentiment_failed or sentiment == agent_runtime.SENTIMENT_URGENT
    calming_ack = _urgent_ack(entities, sentiment_failed) if is_urgent else None

    # Step 2: persist the customer turn (and the ack for urgent turns).
    ticket_status, ticket_sentiment, escalated_meanwhile = _sync_ticket(
        db, ticket_id, caller, existing_status=existing_status, message=message,
        sentiment=sentiment, sentiment_failed=sentiment_failed, calming_ack=calming_ack,
    )
    if escalated_meanwhile:
        return escalated_followup_response(ticket_id, ticket_sentiment)

    # Step 3: urgent / frustrated, or sentiment unavailable: human queue.
    if is_urgent:
        return _human_queue_response(
            ticket_id, ticket_status, calming_ack,
            sentiment if not sentiment_failed else SENTIMENT_UNAVAILABLE,
            entities=entities,
        )

    # Step 4: circuit breaker admission for the FAQ agent.
    admitted_as = _admit_through_breaker()
    if admitted_as is None:
        logger.warning("Circuit breaker OPEN. Bypassing Agent Platform / RAG and routing directly to Human Queue Outage Fallback.")
        return _outage_fallback(db, ticket_id, caller, sentiment, entities, CITATION_BREAKER_OPEN)

    # Step 5: FAQ agent (all attempts failed, or the probe failed: outage fallback).
    faq_data = await _ask_faq_agent(message, ticket_id, caller, is_probe=admitted_as == CircuitState.HALF_OPEN)
    if not faq_data:
        return _outage_fallback(db, ticket_id, caller, sentiment, entities, CITATION_BREAKER_FAILURE)

    return _faq_answer_response(db, ticket_id, caller, sentiment, entities, faq_data)
