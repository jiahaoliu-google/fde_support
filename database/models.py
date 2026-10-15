import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.dialects.postgresql import UUID

Base = declarative_base()

class Ticket(Base):
    __tablename__ = "tickets"

    # UUID Primary Key (works on PostgreSQL & SQLite)
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id = Column(String(255), nullable=False, index=True)
    current_status = Column(String(50), nullable=False, default="NEW", index=True) # NEW, ESCALATED, RESOLVED
    sentiment = Column(String(50), nullable=False, default="NEUTRAL_FACTUAL") # NEUTRAL_FACTUAL, URGENT_FRUSTRATED
    created_at = Column(DateTime, default=datetime.utcnow)

    # One-to-many relationship with Message
    messages = relationship("Message", back_populates="ticket", cascade="all, delete-orphan")

class Message(Base):
    __tablename__ = "messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ticket_id = Column(String(36), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    sender = Column(String(50), nullable=False) # CUSTOMER, AGENT, SYSTEM
    message_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationship back to Ticket
    ticket = relationship("Ticket", back_populates="messages")
