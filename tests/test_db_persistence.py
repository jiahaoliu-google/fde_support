"""Ticket persistence and transaction rollback on write failures (TDD: "transaction
rollbacks occur on write failures").

Runs the real FastAPI app and real ORM models against a throwaway SQLite database
(tests/db_test_utils.py), with the sentiment and FAQ agents faked.

Rollback tests make one SQL statement fail at the driver *after* earlier statements of
the same transaction have executed (e.g. the ticket INSERT ran, then the message INSERT
fails), and check that the earlier statements are undone: no partial ticket, no
orphan message, no half-applied status change, no leaked connection.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

import backend.main as m  # noqa: E402

from backend.clients import agent_runtime  # noqa: E402

from backend.api import deps  # noqa: E402

from backend.db import models  # noqa: E402
from db_test_utils import TempDatabase, fail_sql, statements_like  # noqa: E402

USER = "persistence-user@example.com"


class _AppWithTempDatabase(unittest.TestCase):
    """Real app + empty temp database + fake agents. Each test gets its own database."""

    def setUp(self):
        self.db = TempDatabase()
        self.db.install(m.app)
        m.app.dependency_overrides[deps.get_caller] = lambda: USER
        agent_runtime.rag_circuit_breaker.record_success()
        # A server-side exception becomes a 500 response, as it would in production.
        self.client = TestClient(m.app, raise_server_exceptions=False)
        self.sentiment = agent_runtime.SENTIMENT_NEUTRAL
        self.faq_escalate = False
        self.faq_calls = 0

        async def sentiment(msg):
            return {"sentiment": self.sentiment, "extracted_entities": {"order_id": "123"}}

        async def faq(msg, sid, uid="x"):
            self.faq_calls += 1
            return {"answer": "Here is how.", "citations": [], "grounded": True, "escalate": self.faq_escalate}

        async def delete(*a, **k):
            return True

        self.saved = (agent_runtime.call_sentiment_agent, agent_runtime.call_faq_agent, agent_runtime.delete_faq_session)
        agent_runtime.call_sentiment_agent, agent_runtime.call_faq_agent, agent_runtime.delete_faq_session = sentiment, faq, delete

    def tearDown(self):
        agent_runtime.call_sentiment_agent, agent_runtime.call_faq_agent, agent_runtime.delete_faq_session = self.saved
        m.app.dependency_overrides.pop(deps.get_caller, None)
        self.db.uninstall(m.app)

    def new_ticket(self):
        r = self.client.post("/api/tickets/create")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["ticket_id"]

    def chat(self, message="where is my order", ticket_id=None):
        body = {"message": message}
        if ticket_id:
            body["session_id"] = ticket_id
        return self.client.post("/api/chat", json=body)


# ---------------------------------------------------------------------------
# Persistence through the real ORM and API
# ---------------------------------------------------------------------------
class TestTicketPersistence(_AppWithTempDatabase):
    def test_create_ticket_persists_new_ticket_owned_by_caller(self):
        tid = self.new_ticket()
        self.assertEqual(self.db.state(tid), ("NEW", []))
        with self.db.Session() as s:
            ticket = s.get(models.Ticket, tid)
            self.assertEqual(ticket.customer_id, USER)
            self.assertEqual(ticket.sentiment, "NEUTRAL_FACTUAL")

    def test_neutral_turn_persists_ticket_and_both_messages(self):
        r = self.chat()
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["ticket_id"]
        self.assertEqual(self.db.state(tid), ("IN_PROGRESS", ["AGENT", "CUSTOMER"]))
        with self.db.Session() as s:
            ticket = s.get(models.Ticket, tid)
            self.assertEqual((ticket.customer_id, ticket.sentiment), (USER, agent_runtime.SENTIMENT_NEUTRAL))

    def test_existing_ticket_moves_from_new_to_in_progress(self):
        tid = self.new_ticket()
        self.assertEqual(self.chat(ticket_id=tid).status_code, 200)
        self.assertEqual(self.db.state(tid), ("IN_PROGRESS", ["AGENT", "CUSTOMER"]))

    def test_urgent_turn_persists_escalation_with_system_ack(self):
        self.sentiment = agent_runtime.SENTIMENT_URGENT
        r = self.chat("this is unacceptable, refund me now")
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["ticket_id"]
        self.assertEqual(self.db.state(tid), ("ESCALATED", ["CUSTOMER", "SYSTEM"]))
        self.assertEqual(self.faq_calls, 0)  # urgent tickets bypass the FAQ agent

    def test_resolve_persists_status_and_system_message(self):
        tid = self.new_ticket()
        self.assertEqual(self.client.post(f"/api/tickets/{tid}/resolve").status_code, 200)
        self.assertEqual(self.db.state(tid), ("RESOLVED", ["SYSTEM"]))

    def test_escalated_follow_up_is_stored_without_agent_calls(self):
        self.sentiment = agent_runtime.SENTIMENT_URGENT
        tid = self.chat("angry").json()["ticket_id"]
        self.sentiment = agent_runtime.SENTIMENT_NEUTRAL
        r = self.chat("any update?", ticket_id=tid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.db.state(tid), ("ESCALATED", ["CUSTOMER", "CUSTOMER", "SYSTEM"]))
        self.assertEqual(self.faq_calls, 0)


# ---------------------------------------------------------------------------
# Rollback on write failures
# ---------------------------------------------------------------------------
class TestRollbackOnWriteFailure(_AppWithTempDatabase):
    def assert_failed_cleanly(self, response, probe):
        self.assertEqual(response.status_code, 500, response.text)
        self.assertIsNotNone(probe["failed"], "the injected failure never triggered")
        self.assertEqual(self.db.connections_in_use(), 0, "connection leaked after the failure")

    def test_new_ticket_message_insert_fails_ticket_insert_is_rolled_back(self):
        with fail_sql(self.db.engine, "INSERT INTO messages") as probe:
            r = self.chat()
        self.assert_failed_cleanly(r, probe)
        self.assertTrue(statements_like(probe, "INSERT INTO tickets"), "ticket INSERT should run before the failure")
        self.assertEqual(self.db.counts(), (0, 0))  # no ticket without its message

    def test_urgent_turn_write_fails_no_partial_escalation(self):
        self.sentiment = agent_runtime.SENTIMENT_URGENT
        with fail_sql(self.db.engine, "INSERT INTO messages") as probe:
            r = self.chat("refund me now")
        self.assert_failed_cleanly(r, probe)
        self.assertEqual(self.db.counts(), (0, 0))  # no ESCALATED ticket without CUSTOMER + SYSTEM

    def test_existing_ticket_status_update_is_rolled_back(self):
        tid = self.new_ticket()
        with fail_sql(self.db.engine, "INSERT INTO messages") as probe:
            r = self.chat(ticket_id=tid)
        self.assert_failed_cleanly(r, probe)
        self.assertTrue(statements_like(probe, "UPDATE tickets"), "status UPDATE should run before the failure")
        self.assertEqual(self.db.state(tid), ("NEW", []))  # NEW -> IN_PROGRESS undone

    def test_agent_reply_write_fails_earlier_commit_kept_escalation_undone(self):
        """Step 2 (customer message) is its own committed transaction; only the failed
        agent-reply transaction is rolled back, including its ESCALATED status change."""
        tid = self.new_ticket()
        self.faq_escalate = True
        with fail_sql(self.db.engine, "INSERT INTO messages", nth=2) as probe:  # 1 = customer, 2 = agent
            r = self.chat(ticket_id=tid)
        self.assert_failed_cleanly(r, probe)
        self.assertEqual(self.db.state(tid), ("IN_PROGRESS", ["CUSTOMER"]))

    def test_outage_fallback_write_fails_escalation_undone(self):
        tid = self.new_ticket()
        with mock.patch.object(agent_runtime.rag_circuit_breaker, "try_acquire", return_value=None), \
                fail_sql(self.db.engine, "INSERT INTO messages", nth=2) as probe:  # 2 = SYSTEM fallback
            r = self.chat(ticket_id=tid)
        self.assert_failed_cleanly(r, probe)
        self.assertEqual(self.db.state(tid), ("IN_PROGRESS", ["CUSTOMER"]))

    def test_escalated_follow_up_write_fails_nothing_appended(self):
        self.sentiment = agent_runtime.SENTIMENT_URGENT
        tid = self.chat("angry").json()["ticket_id"]
        with fail_sql(self.db.engine, "INSERT INTO messages") as probe:
            r = self.chat("any update?", ticket_id=tid)
        self.assert_failed_cleanly(r, probe)
        self.assertEqual(self.db.state(tid), ("ESCALATED", ["CUSTOMER", "SYSTEM"]))

    def test_resolve_write_fails_status_update_is_rolled_back(self):
        tid = self.new_ticket()
        with fail_sql(self.db.engine, "INSERT INTO messages") as probe:
            r = self.client.post(f"/api/tickets/{tid}/resolve")
        self.assert_failed_cleanly(r, probe)
        self.assertTrue(statements_like(probe, "UPDATE tickets"))
        self.assertEqual(self.db.state(tid), ("NEW", []))  # not RESOLVED without its SYSTEM message

    def test_create_ticket_write_fails_nothing_persisted(self):
        with fail_sql(self.db.engine, "INSERT INTO tickets") as probe:
            r = self.client.post("/api/tickets/create")
        self.assert_failed_cleanly(r, probe)
        self.assertEqual(self.db.counts(), (0, 0))

    def test_ticket_works_normally_after_a_failed_write(self):
        tid = self.new_ticket()
        with fail_sql(self.db.engine, "INSERT INTO messages"):
            self.chat(ticket_id=tid)
        r = self.chat(ticket_id=tid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.db.state(tid), ("IN_PROGRESS", ["AGENT", "CUSTOMER"]))


if __name__ == "__main__":
    unittest.main()
