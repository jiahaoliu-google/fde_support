"""Ticket routes: create, resolve, list, and fetch a ticket with its messages."""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.deps import agent_user_id, get_caller
from backend.api.schemas import CreateTicketRequest
from backend.clients import agent_runtime
from backend.db.models import Message, Ticket
from backend.db.session import get_db
from backend.services.tickets import (
    get_owned_ticket,
    is_ticket_session_expired,
    new_ticket_id,
)

logger = logging.getLogger("backend")

router = APIRouter()

@router.get("/api/tickets")
async def list_tickets(db: Session = Depends(get_db), caller: str = Depends(get_caller)):
    """Lists previous tickets owned by the caller, newest first, with agent session expiry status."""
    with db.begin():
        rows = (
            db.query(Ticket)
            .filter(Ticket.customer_id == caller)
            .order_by(Ticket.created_at.desc())
            .limit(50)
            .all()
        )
        ticket_ids = [t.id for t in rows]
        msg_rows = (
            db.query(Message)
            .filter(Message.ticket_id.in_(ticket_ids))
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
            if ticket_ids
            else []
        )
        first_user_msg: dict[str, str] = {}
        msg_counts: dict[str, int] = {}
        for m in msg_rows:
            msg_counts[m.ticket_id] = msg_counts.get(m.ticket_id, 0) + 1
            if m.sender == "CUSTOMER" and m.ticket_id not in first_user_msg:
                first_user_msg[m.ticket_id] = m.message_text

        ticket_snapshots = [
            {
                "id": t.id,
                "customer_id": t.customer_id,
                "current_status": t.current_status,
                "sentiment": t.sentiment,
                "created_at": t.created_at,
                "message_count": msg_counts.get(t.id, 0),
                "preview": (first_user_msg.get(t.id, "") or "")[:80],
            }
            for t in rows
            if msg_counts.get(t.id, 0) > 0 or t.current_status != "NEW"
        ]

    # Query the Agent Platform outside any DB transaction so no pooled connection is held.
    has_in_progress = any(t["current_status"] == "IN_PROGRESS" for t in ticket_snapshots)
    active_sessions = (
        await agent_runtime.list_faq_sessions(agent_user_id(caller))
        if has_in_progress
        else None
    )

    tickets_out = []
    for snap in ticket_snapshots:
        expired = is_ticket_session_expired(
            snap["id"], snap["current_status"], snap["created_at"], active_sessions
        )
        tickets_out.append({**snap, "session_expired": expired})

    return {"tickets": tickets_out}

@router.post("/api/tickets/create")
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

@router.post("/api/tickets/{ticket_id}/resolve")
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
    await agent_runtime.delete_faq_session(ticket_id, agent_user_id(caller))

    return {
        "status": "success",
        "ticket_id": ticket_id,
        "current_status": "RESOLVED",
        "message": f"Ticket {ticket_id} has been marked as RESOLVED."
    }

@router.get("/api/tickets/{ticket_id}")
async def get_ticket_details(ticket_id: str, db: Session = Depends(get_db), caller: str = Depends(get_caller)):
    with db.begin():
        ticket = get_owned_ticket(db, ticket_id, caller)
        messages = (
            db.query(Message)
            .filter(Message.ticket_id == ticket.id)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )
        ticket_data = {
            "id": ticket.id,
            "customer_id": ticket.customer_id,
            "current_status": ticket.current_status,
            "sentiment": ticket.sentiment,
            "created_at": ticket.created_at,
        }
        messages_data = [
            {
                "id": m.id,
                "sender": m.sender,
                "message_text": m.message_text,
                "created_at": m.created_at,
            }
            for m in messages
        ]

    active_sessions = (
        await agent_runtime.list_faq_sessions(agent_user_id(caller))
        if ticket_data["current_status"] == "IN_PROGRESS"
        else None
    )
    ticket_data["session_expired"] = is_ticket_session_expired(
        ticket_data["id"],
        ticket_data["current_status"],
        ticket_data["created_at"],
        active_sessions,
    )

    return {
        "ticket": ticket_data,
        "messages": messages_data,
    }
