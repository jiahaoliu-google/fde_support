"""Ticket ownership checks and transactional writes of ticket turns."""
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.db.models import Message, Ticket

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
