"""Transaction boundaries in /api/chat and /api/tickets/{id}/resolve.

* No pooled DB connection (and so no open transaction) is held while an agent is called.
* A ticket resolved while the FAQ agent is running is not reopened.
* A ticket escalated meanwhile is never downgraded.
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

import backend.main as m  # noqa: E402
from database.models import Ticket  # noqa: E402
from db_test_utils import TempDatabase  # noqa: E402

USER = "txn-user@example.com"


class TestTransactionBoundaries(unittest.TestCase):
    def setUp(self):
        self.saved = (m.call_sentiment_agent, m.call_faq_agent, m.delete_faq_session)
        self.db = TempDatabase()
        self.db.install(m.app)
        m.app.dependency_overrides[m.get_caller] = lambda: USER
        m.rag_circuit_breaker.record_success()
        self.client = TestClient(m.app)
        self.checked_out = []
        self.on_faq = None
        self.faq_escalate = False

        async def sentiment(msg):
            self.checked_out.append(("sentiment", self.db.connections_in_use()))
            return {"sentiment": m.SENTIMENT_NEUTRAL}

        async def faq(msg, sid, uid="x"):
            self.checked_out.append(("faq", self.db.connections_in_use()))
            if self.on_faq:
                self.on_faq(sid)
            return {"answer": "hello", "citations": [], "grounded": True, "escalate": self.faq_escalate}

        async def delete(*a, **k):
            return True

        m.call_sentiment_agent, m.call_faq_agent, m.delete_faq_session = sentiment, faq, delete

    def tearDown(self):
        m.call_sentiment_agent, m.call_faq_agent, m.delete_faq_session = self.saved
        m.app.dependency_overrides.pop(m.get_caller, None)
        self.db.uninstall(m.app)

    def _ticket(self):
        return self.client.post("/api/tickets/create").json()["ticket_id"]

    def _state(self, tid):
        status, senders = self.db.state(tid)
        return status, sorted(senders)

    def test_no_connection_held_during_agent_calls(self):
        for session_id in (self._ticket(), None):  # existing ticket and brand-new ticket
            self.checked_out.clear()
            body = {"message": "where is my order"}
            if session_id:
                body["session_id"] = session_id
            r = self.client.post("/api/chat", json=body)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.checked_out, [("sentiment", 0), ("faq", 0)])

    def test_resolved_during_faq_call_is_not_reopened(self):
        tid = self._ticket()
        self.faq_escalate = True  # the stale write would have set ESCALATED
        self.on_faq = lambda sid: self.client.post(f"/api/tickets/{sid}/resolve")
        r = self.client.post("/api/chat", json={"message": "hi", "session_id": tid})
        self.assertEqual(r.status_code, 400)
        self.assertIn("RESOLVED", r.json()["detail"])  # the UI locks the chat on this
        status, senders = self._state(tid)
        self.assertEqual(status, "RESOLVED")
        self.assertNotIn("AGENT", senders)

    def test_escalated_during_faq_call_is_not_downgraded(self):
        tid = self._ticket()

        def escalate(sid):
            with self.db.Session() as s, s.begin():
                s.get(Ticket, sid).current_status = "ESCALATED"

        self.on_faq = escalate
        r = self.client.post("/api/chat", json={"message": "hi", "session_id": tid})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ticket_status"], "ESCALATED")
        self.assertEqual(self._state(tid)[0], "ESCALATED")

    def test_customer_message_and_status_written_together(self):
        tid = self._ticket()
        r = self.client.post("/api/chat", json={"message": "hi", "session_id": tid})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._state(tid), ("IN_PROGRESS", ["AGENT", "CUSTOMER"]))


if __name__ == "__main__":
    unittest.main()
