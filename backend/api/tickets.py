"""Ticket routes: create, resolve, and fetch a ticket with its messages."""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.deps import agent_user_id, get_caller
from backend.api.schemas import CreateTicketRequest
from backend.clients import agent_runtime
from backend.db.models import Message, Ticket
from backend.db.session import get_db
from backend.services.tickets import get_owned_ticket, new_ticket_id

logger = logging.getLogger("backend")

router = APIRouter()

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
