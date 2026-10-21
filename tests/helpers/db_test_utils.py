"""Isolated databases and write-failure injection for backend tests.

Not a test module (no ``test_`` prefix), so unittest discovery doesn't collect it.

TempDatabase gives each test class its own empty SQLite file and wires it into the app
by overriding the ``get_db`` dependency. Every DB access in the backend goes through
that dependency, so the tests never touch ./test_local.db, local_dev.db or Cloud SQL,
and don't depend on which test module imported ``backend.db.session`` first.

fail_sql makes the Nth SQL statement that starts with a given prefix raise
OperationalError at the driver boundary, i.e. after earlier statements of the same
transaction really ran. That is how the rollback tests prove those earlier writes are
undone, not merely never sent.
"""
import os
import shutil
import tempfile
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.db.session import get_db
from backend.db.models import Base, Message, Ticket

from tests.helpers import no_network  # noqa: F401  (blocks outbound network for every test using these helpers)


class TempDatabase:
    """A fresh SQLite database file, created with the real ORM models."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="fde-test-db-")
        self.engine = create_engine(
            f"sqlite:///{os.path.join(self.dir, 'test.db')}",
            connect_args={"check_same_thread": False},  # TestClient runs the app in a worker thread
        )
        Base.metadata.create_all(self.engine)
        # Same session options as backend.db.session.SessionLocal.
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def get_db(self):
        db = self.Session()
        try:
            yield db
        finally:
            db.close()

    def install(self, app):
        app.dependency_overrides[get_db] = self.get_db

    def uninstall(self, app):
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()
        shutil.rmtree(self.dir, ignore_errors=True)

    def state(self, ticket_id):
        """(status or None if the ticket doesn't exist, sorted message senders).

        Sorted because Message.id is a random UUID: rows have no reliable insert order,
        and these tests check which rows exist, not their order.
        """
        with self.Session() as s:
            ticket = s.get(Ticket, ticket_id)
            senders = sorted(m.sender for m in s.query(Message).filter(Message.ticket_id == ticket_id))
            return (ticket.current_status if ticket else None), senders

    def counts(self):
        """(number of tickets, number of messages) in the whole database."""
        with self.Session() as s:
            return s.query(Ticket).count(), s.query(Message).count()

    def connections_in_use(self):
        return self.engine.pool.checkedout()


@contextmanager
def fail_sql(engine, prefix, nth=1):
    """Raises OperationalError on the nth SQL statement starting with ``prefix``.

    Yields a dict: ``executed`` lists the statements that reached the driver before the
    failure (in order), ``failed`` is the statement that raised, or None if none matched.
    """
    probe = {"executed": [], "failed": None, "matches": 0}

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        text = " ".join(statement.split())
        if text.upper().startswith(prefix.upper()):
            probe["matches"] += 1
            if probe["matches"] == nth:
                probe["failed"] = text
                raise OperationalError(statement, parameters, Exception("injected write failure"))
        probe["executed"].append(text)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield probe
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def statements_like(probe, prefix):
    return [s for s in probe["executed"] if s.upper().startswith(prefix.upper())]
