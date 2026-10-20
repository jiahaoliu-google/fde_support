"""Branches of the /api/chat pipeline that other suites don't reach.

* PII is redacted before any agent call and before storage.
* A ticket resolved or escalated by another request while the sentiment agent runs.
* A ticket resolved between the ownership check and the escalated follow-up write.
* FAQ answers that wrap their JSON reply in a markdown fence, or send malformed JSON.

Everything goes through HTTP so the tests pin observable behavior, not module layout.
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import backend.main as m  # noqa: E402
from backend.api.deps import get_caller  # noqa: E402
from backend.clients import agent_runtime as AR  # noqa: E402
from backend.services import chat as CHAT  # noqa: E402
from db_test_utils import TempDatabase  # noqa: E402

USER = "branches-user@example.com"
FOLLOWUP_ACK = (
    "Your ticket is with our human support team. We've added your message to it, "
    "and a specialist will reply here."
)


class ChatPipelineCase(unittest.TestCase):
    def setUp(self):
        self.saved = (AR.call_sentiment_agent, AR.call_faq_agent, AR.delete_faq_session)
        self.db = TempDatabase()
        self.db.install(m.app)
        m.app.dependency_overrides[get_caller] = lambda: USER
        AR.rag_circuit_breaker.record_success()
        self.client = TestClient(m.app)

        self.sentiment_label = AR.SENTIMENT_NEUTRAL
        self.on_sentiment = None
        self.faq_answer = {"answer": "hello", "citations": [], "grounded": True, "escalate": False}
        self.agent_inputs = []

        async def sentiment(msg):
            self.agent_inputs.append(("sentiment", msg))
            if self.on_sentiment:
                self.on_sentiment()
            return {"sentiment": self.sentiment_label, "extracted_entities": {}}

        async def faq(msg, sid, uid="x"):
            self.agent_inputs.append(("faq", msg))
            return dict(self.faq_answer)

        async def delete(*a, **k):
            return True

        AR.call_sentiment_agent, AR.call_faq_agent, AR.delete_faq_session = sentiment, faq, delete

    def tearDown(self):
        AR.call_sentiment_agent, AR.call_faq_agent, AR.delete_faq_session = self.saved
        m.app.dependency_overrides.pop(get_caller, None)
        self.db.uninstall(m.app)

    # -- helpers ----------------------------------------------------------
    def _ticket(self):
        return self.client.post("/api/tickets/create").json()["ticket_id"]

    def _set_status(self, tid, status):
        with self.db.engine.begin() as conn:
            conn.execute(text("UPDATE tickets SET current_status = :s WHERE id = :t"), {"s": status, "t": tid})

    def _messages(self, tid):
        with self.db.engine.connect() as conn:
            rows = conn.execute(text("SELECT sender, message_text FROM messages WHERE ticket_id = :t"), {"t": tid})
            return sorted((r[0], r[1]) for r in rows)

    def _chat(self, message, tid=None):
        body = {"message": message}
        if tid:
            body["session_id"] = tid
        return self.client.post("/api/chat", json=body)

    def _agents_called(self):
        return [name for name, _ in self.agent_inputs]


class TestRedaction(ChatPipelineCase):
    def test_pii_redacted_before_agents_and_storage(self):
        tid = self._ticket()
        r = self._chat("my email is jane.doe@example.com and card 4111 1111 1111 1111", tid)
        self.assertEqual(r.status_code, 200, r.text)

        for _, sent in self.agent_inputs:
            self.assertNotIn("jane.doe@example.com", sent)
            self.assertNotIn("4111 1111 1111 1111", sent)
        stored = [t for s, t in self._messages(tid) if s == "CUSTOMER"]
        self.assertEqual(len(stored), 1)
        self.assertNotIn("jane.doe@example.com", stored[0])
        self.assertNotIn("4111", stored[0])


class TestConcurrentStatusChanges(ChatPipelineCase):
    def test_resolved_while_sentiment_runs(self):
        tid = self._ticket()
        self._chat("first question", tid)  # ticket is now IN_PROGRESS
        before = self._messages(tid)
        self.agent_inputs.clear()

        self.on_sentiment = lambda: self._set_status(tid, "RESOLVED")
        r = self._chat("second question", tid)

        self.assertEqual(r.status_code, 400)
        self.assertIn("RESOLVED while your message was being processed", r.json()["detail"])
        self.assertEqual(self._agents_called(), ["sentiment"])  # FAQ never asked
        self.assertEqual(self._messages(tid), before)            # nothing written

    def test_escalated_while_sentiment_runs_non_urgent(self):
        tid = self._ticket()
        self._chat("first question", tid)
        self.agent_inputs.clear()

        self.on_sentiment = lambda: self._set_status(tid, "ESCALATED")
        r = self._chat("second question", tid)

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["ticket_status"], "ESCALATED")
        self.assertEqual(body["answer"], FOLLOWUP_ACK)
        self.assertEqual(body["routed_to"], "Human Escalation Queue")
        self.assertEqual(body["citations"], ["Assigned to Human Support Specialist Queue"])
        self.assertFalse(body["grounded"])
        self.assertEqual(body["agent_service_version"], "v2.5-human-escalation")
        self.assertEqual(self._agents_called(), ["sentiment"])  # FAQ never asked
        # The customer turn is kept for the human; the ack itself is not stored.
        senders = [s for s, _ in self._messages(tid)]
        self.assertEqual(senders.count("CUSTOMER"), 2)
        self.assertEqual(senders.count("SYSTEM"), 0)

    def test_escalated_while_sentiment_runs_urgent(self):
        tid = self._ticket()
        self._chat("first question", tid)
        self.agent_inputs.clear()

        self.sentiment_label = AR.SENTIMENT_URGENT
        self.on_sentiment = lambda: self._set_status(tid, "ESCALATED")
        r = self._chat("this is unacceptable", tid)

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["ticket_status"], "ESCALATED")
        self.assertEqual(body["sentiment"], AR.SENTIMENT_URGENT)
        self.assertIn("prioritized as URGENT", body["answer"])
        self.assertEqual(self._agents_called(), ["sentiment"])
        senders = [s for s, _ in self._messages(tid)]
        self.assertEqual(senders.count("SYSTEM"), 1)  # urgent ack stored with the turn

    def test_escalated_ticket_resolved_before_followup_write(self):
        tid = self._ticket()
        self._set_status(tid, "ESCALATED")
        before = self._messages(tid)
        real_redact = CHAT.model_armor.redact

        def redact_then_resolve(message):
            # Runs after the ownership check saw ESCALATED, before the follow-up write.
            self._set_status(tid, "RESOLVED")
            return real_redact(message)

        with mock.patch.object(CHAT.model_armor, "redact", side_effect=redact_then_resolve):
            r = self._chat("any update?", tid)

        self.assertEqual(r.status_code, 400)
        self.assertIn("marked as RESOLVED", r.json()["detail"])
        self.assertEqual(self.agent_inputs, [])  # escalated tickets never reach the agents
        self.assertEqual(self._messages(tid), before)


class TestFaqAnswerUnwrapping(ChatPipelineCase):
    def test_json_fenced_answer_is_unwrapped_and_overrides_flags(self):
        self.faq_answer = {
            "answer": '```json\n{"answer": "Restart the router.", "citations": ["twcs_42"], '
                      '"grounded": false, "escalate": true}\n```',
            "citations": ["twcs_1"],
            "grounded": True,
            "escalate": False,
        }
        tid = self._ticket()
        r = self._chat("wifi is down", tid)

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["answer"], "Restart the router.")
        self.assertFalse(body["grounded"])
        self.assertEqual(body["ticket_status"], "ESCALATED")
        self.assertEqual(body["routed_to"], "Automated RAG Assistant ➔ Human Escalation Queue")
        self.assertEqual(len(body["citations"]), 2)  # outer + inner citation merged
        self.assertIn(("AGENT", "Restart the router."), self._messages(tid))

    def test_plain_fenced_answer_is_unwrapped(self):
        self.faq_answer = {
            "answer": '```\n{"answer": "Check your spam folder.", "citations": []}\n```',
            "citations": [],
            "grounded": True,
            "escalate": False,
        }
        r = self._chat("no confirmation email", self._ticket())

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["answer"], "Check your spam folder.")
        self.assertTrue(body["grounded"])
        self.assertEqual(body["ticket_status"], "IN_PROGRESS")
        self.assertEqual(body["routed_to"], "Automated RAG Assistant")

    def test_malformed_inner_json_answer_is_salvaged(self):
        # Invalid JSON (bare identifier in the list), so the regex fallback extracts the answer
        # and un-escapes \n and \".
        self.faq_answer = {
            "answer": r'{"answer": "Say \"hi\" to the agent\nthen wait", "citations": [twcs_7]}',
            "citations": [],
            "grounded": True,
            "escalate": False,
        }
        r = self._chat("how do I reach someone", self._ticket())

        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["answer"], 'Say "hi" to the agent\nthen wait')


if __name__ == "__main__":
    unittest.main()
