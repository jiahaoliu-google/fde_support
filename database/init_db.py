import os
import sys
import sqlite3
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

DB_PATH = os.getenv("SQLITE_DB_PATH", os.path.join(os.path.dirname(__file__), "fde_support.db"))

SQLITE_SCHEMA = """
-- SQLite compatible schema for local development
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    current_status TEXT NOT NULL DEFAULT 'NEW',
    sentiment TEXT NOT NULL DEFAULT 'NEUTRAL_FACTUAL',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    sender TEXT NOT NULL,
    message_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tickets_customer_id ON tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(current_status);
CREATE INDEX IF NOT EXISTS idx_messages_ticket_id ON messages(ticket_id);
"""

def init_sqlite_db(db_file=DB_PATH):
    """Initializes local SQLite database with tickets and messages tables."""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.executescript(SQLITE_SCHEMA)
    conn.commit()
    conn.close()
    logger.info(f"SQLite database initialized successfully at: {db_file}")

if __name__ == "__main__":
    init_sqlite_db()
