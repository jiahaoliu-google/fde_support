"""The backend steps of /api/chat that the agent eval also times.

  * Model Armor redaction: no TDD / Scoping target (reported only)
  * Cloud SQL ticket-state writes: T3, TDD North Star: p95 <= 100 ms

It calls the production functions themselves (backend.services.chat._redact and
_sync_ticket, backend.services.tickets.record_turn) on an eval database, so the timings
are of the code that ships, not of a copy.

Database:
  * default: a throw-away SQLite file in a temp dir (deleted on close). Measured and
    shown, but T3 is not judged: the target is defined for Cloud SQL.
  * --db-url postgresql+psycopg2://...: e.g. Cloud SQL through cloud-sql-proxy on
    127.0.0.1. T3 is judged. Rows are written under a unique `eval-<hex>` customer id
    and deleted on close unless keep_rows=True.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import threading
import time
import uuid
from typing import Callable, Optional

# Writes per turn, named for raw_results.jsonl.
OP_SYNC_TICKET = "sync_ticket"            # customer message (+ urgent ack), ticket create/update
OP_AGENT_REPLY = "record_agent_reply"     # FAQ answer
OP_FALLBACK = "record_outage_fallback"    # FAQ agent failed: human-queue fallback


class BackendProbe:
    """Runs and times redaction and ticket writes for one eval run."""

    def __init__(self, db_url: Optional[str] = None, keep_rows: bool = False):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from backend.db.models import Base
        from backend.security.model_armor import model_armor
        from backend.services import chat, tickets

        self._tmpdir = None
        if not db_url:
            self._tmpdir = tempfile.mkdtemp(prefix="fde-eval-db-")
            db_url = f"sqlite:///{os.path.join(self._tmpdir, 'eval.db')}"
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        self.engine = create_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
        self.dialect = self.engine.dialect.name
        self.url = self.engine.url.render_as_string(hide_password=True)  # never log the password
        Base.metadata.create_all(self.engine)
        # Same session options as backend.db.session.SessionLocal.
        self._session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        # SQLite allows one writer at a time. Serialize writes and start the clock after
        # the lock, so a wait for another case's write isn't counted as write latency.
        self._lock = threading.Lock() if self.dialect == "sqlite" else None
        self.customer_id = f"eval-{uuid.uuid4().hex[:12]}"
        self.keep_rows = keep_rows
        self._chat, self._tickets = chat, tickets
        self._model_armor = model_armor
        # Configured state; each message records the path that actually ran.
        self.model_armor_path = model_armor.dlp_status()

    # ------------------------------------------------------------------ info
    def info(self) -> dict:
        return {"dialect": self.dialect, "url": self.url, "model_armor_path": self.model_armor_path}

    def new_ticket_id(self) -> str:
        return self._tickets.new_ticket_id()

    def urgent_ack(self, sentiment_failed: bool) -> str:
        return self._chat._urgent_ack({}, sentiment_failed)

    @property
    def outage_fallback_answer(self) -> str:
        return self._chat.OUTAGE_FALLBACK_ANSWER

    # ------------------------------------------------------------- redaction
    async def redact(self, message: str) -> tuple[str, dict]:
        """(redacted message, {latency_ms, changed, path}). In a worker thread because
        the DLP path is a blocking network call. `path` is "dlp" or "regex": what ran."""
        def run():
            start = time.perf_counter()
            out, path = self._model_armor.redact_with_path(message)
            return out, path, (time.perf_counter() - start) * 1000.0
        out, path, ms = await asyncio.to_thread(run)
        return out, {"latency_ms": ms, "changed": out != message, "path": path}

    # ---------------------------------------------------------------- writes
    def _timed_write(self, op: str, fn: Callable) -> tuple[object, dict]:
        rec = {"op": op, "latency_ms": None, "error": None}
        result = None
        with (self._lock or contextlib.nullcontext()):
            db = self._session()
            try:
                start = time.perf_counter()
                result = fn(db)
                rec["latency_ms"] = (time.perf_counter() - start) * 1000.0
            except Exception as e:  # noqa: BLE001 - a failed write is a measured outcome
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            finally:
                db.close()
        return result, rec

    async def sync_ticket(self, ticket_id: str, *, existing_status: Optional[str], message: str,
                          sentiment: str, sentiment_failed: bool, calming_ack: Optional[str]) -> tuple[Optional[str], dict]:
        """chat._sync_ticket: (ticket status or None on error, write record)."""
        def fn(db):
            status, _, _ = self._chat._sync_ticket(
                db, ticket_id, self.customer_id, existing_status=existing_status, message=message,
                sentiment=sentiment, sentiment_failed=sentiment_failed, calming_ack=calming_ack)
            return status
        return await asyncio.to_thread(self._timed_write, OP_SYNC_TICKET, fn)

    async def record_turn(self, op: str, ticket_id: str, sender: str, text: str,
                          escalate: bool) -> tuple[Optional[str], dict]:
        """tickets.record_turn: (ticket status or None on error, write record)."""
        def fn(db):
            return self._tickets.record_turn(db, ticket_id, self.customer_id, sender, text, escalate=escalate)
        return await asyncio.to_thread(self._timed_write, op, fn)

    # --------------------------------------------------------------- cleanup
    def close(self) -> None:
        """Deletes the temp SQLite dir, or this run's rows on an external database."""
        try:
            if self._tmpdir is None and not self.keep_rows:
                from sqlalchemy import delete, select

                from backend.db.models import Message, Ticket
                with self._session() as db, db.begin():
                    ids = select(Ticket.id).where(Ticket.customer_id == self.customer_id)
                    db.execute(delete(Message).where(Message.ticket_id.in_(ids)))
                    db.execute(delete(Ticket).where(Ticket.customer_id == self.customer_id))
        finally:
            self.engine.dispose()
            if self._tmpdir:
                shutil.rmtree(self._tmpdir, ignore_errors=True)
