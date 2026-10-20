"""Request and response models for the backend API."""
from pydantic import BaseModel, Field


class CreateTicketRequest(BaseModel):
    """Empty body: ticket IDs are generated server-side and identity comes from auth.

    Unknown keys (e.g. a legacy session_id/customer_id) are ignored by Pydantic.
    """

class SupportQueryRequest(BaseModel):
    message: str = Field(..., max_length=4000, examples=["Where is my order #12345?"])
    # Ticket ID returned by /api/tickets/create. Omit to open a new ticket.
    session_id: str | None = Field(default=None, max_length=64, examples=["3f0c...uuid"])

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
