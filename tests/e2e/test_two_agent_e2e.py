"""End-to-end: /api/chat -> Sentiment Router agent -> FAQ Assistant agent -> ticket DB.

Each test drives the real backend over HTTP. The backend's agent client calls the two
real ADK agents (served in-process by tests/e2e/agent_harness.py) through their
Agent Runtime routes, so every hop in between is production code: request payloads,
the agents' prompts / schemas / tools / callbacks, ADK event JSON, NDJSON parsing,
routing, and the ticket writes. Only Gemini and Discovery Engine are scripted.
"""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

import backend.main as m  # noqa: E402
from backend.api.deps import agent_user_id, get_caller  # noqa: E402
from backend.clients import agent_runtime as AR  # noqa: E402
from tests.helpers.db_test_utils import TempDatabase  # noqa: E402
from tests.e2e import agent_harness as H  # noqa: E402

# conftest.py replaces this with an AsyncMock for every test; keep the real one.
REAL_DELETE_FAQ_SESSION = AR.delete_faq_session

USER = "e2e-customer@example.com"


class TwoAgentE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # FAQ sessions in memory, never the Vertex AI session service.
        cls._env = mock.patch.dict(os.environ, {"REQUIRE_DURABLE_SESSIONS": "false"})
        cls._env.start()
        for name in ("GOOGLE_CLOUD_AGENT_ENGINE_ID", "SESSION_SERVICE_URI", "GOOGLE_API_CERTIFICATE_CONFIG"):
            os.environ.pop(name, None)
        cls.servers = H.agent_servers()

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()

    def setUp(self):
        self.sentiment = self.servers[H.SENTIMENT_ENGINE]
        self.faq = self.servers[H.FAQ_ENGINE]
        self.sentiment_llm = H.ScriptedSentimentLlm()
        self.faq_llm = H.ScriptedFaqLlm()
        self.search_queries = []
        self.router = H.AgentRouter({e: s.app for e, s in self.servers.items()})

        patches = [
            mock.patch.object(self.sentiment.agent, "model", self.sentiment_llm),
            mock.patch.object(self.faq.agent, "model", self.faq_llm),
            mock.patch("agents.faq_assistant.tools._get_search_client",
                       lambda: H.FakeSearchClient(self.search_queries)),
            mock.patch.object(AR, "httpx", H.httpx_via(self.router)),
            mock.patch.object(AR, "SENTIMENT_AGENT_URL", H.SENTIMENT_AGENT_URL),
            mock.patch.object(AR, "FAQ_AGENT_URL", H.FAQ_AGENT_URL),
            mock.patch.object(AR, "delete_faq_session", REAL_DELETE_FAQ_SESSION),
            mock.patch("backend.clients.gcp_auth.get_gcp_auth_headers", lambda: {}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.db = TempDatabase()
        self.db.install(m.app)
        m.app.dependency_overrides[get_caller] = lambda: USER
        AR.rag_circuit_breaker.record_success()
        self.client = TestClient(m.app)

    def tearDown(self):
        m.app.dependency_overrides.pop(get_caller, None)
        self.db.uninstall(m.app)

    # -- helpers ----------------------------------------------------------
    def _new_ticket(self):
        return self.client.post("/api/tickets/create").json()["ticket_id"]

    def _chat(self, message, ticket_id):
        r = self.client.post("/api/chat", json={"message": message, "session_id": ticket_id})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _faq_session(self, ticket_id):
        store = self.faq.services.get_session_service()
        return asyncio.run(store.get_session(
            app_name=self.faq.agent_app_name, user_id=agent_user_id(USER),
            session_id=AR.agent_session_id(ticket_id)))

    # -- tests ------------------------------------------------------------
    def test_neutral_order_question_flows_through_both_agents(self):
        tid = self._new_ticket()
        out = self._chat("Hi, where is my order #10201?", tid)

        self.assertEqual(self.router.engines_called(), [H.SENTIMENT_ENGINE, H.FAQ_ENGINE])
        self.assertEqual(out["sentiment"], "NEUTRAL_FACTUAL")
        self.assertEqual(out["routed_to"], AR_ROUTE_FAQ())
        self.assertEqual(out["ticket_status"], "IN_PROGRESS")
        self.assertTrue(out["grounded"])
        # Facts come from the real get_order_status tool / mock order DB.
        self.assertIn("1Z999AA10123456791", out["answer"])
        self.assertIn("2026-10-19", out["answer"])
        self.assertEqual(out["extracted_entities"].get("order_id"), "10201")

        status, senders = self.db.state(tid)
        self.assertEqual(status, "IN_PROGRESS")
        self.assertEqual(senders, ["AGENT", "CUSTOMER"])
        # Sentiment classification sessions are deleted after each run.
        self.assertEqual(self.sentiment.services.get_session_service().session_count(), 0)

    def test_urgent_message_escalates_without_calling_faq_agent(self):
        tid = self._new_ticket()
        out = self._chat("This is UNACCEPTABLE, I was charged twice for order #10501!!!", tid)

        self.assertEqual(self.router.engines_called(), [H.SENTIMENT_ENGINE])
        self.assertEqual(out["sentiment"], "URGENT_FRUSTRATED")
        self.assertEqual(out["ticket_status"], "ESCALATED")
        self.assertFalse(out["grounded"])
        self.assertEqual(self.faq_llm.requests, [])
        self.assertEqual(self.db.state(tid)[0], "ESCALATED")

    def test_faq_agent_remembers_earlier_turns_of_the_ticket(self):
        tid = self._new_ticket()
        first = self._chat("Where is my package?", tid)
        self.assertIn("order ID", first["answer"])

        second = self._chat("It's 10301", tid)
        self.assertIn("out for delivery", second["answer"])

        faq_calls = [body for engine, _, body in self.router.calls if engine == H.FAQ_ENGINE]
        self.assertEqual(len(faq_calls), 2)
        self.assertEqual(faq_calls[0]["input"]["session_id"], faq_calls[1]["input"]["session_id"])
        # The second model call saw the first turn: memory lives in the agent session.
        self.assertIn("Where is my package?", H.request_text(self.faq_llm.requests[-1]))
        self.assertIsNotNone(self._faq_session(tid))

    def test_policy_answer_uses_rag_and_hides_document_ids(self):
        tid = self._new_ticket()
        out = self._chat("What is the return policy for headphones?", tid)

        self.assertEqual(len(self.search_queries), 1)
        self.assertTrue(out["grounded"])
        self.assertIn("30 days", out["answer"])
        # The scripted model leaked "(doc_id: twcs_400101)"; the agent callback removed it.
        self.assertNotIn("twcs_", out["answer"])

    def test_action_request_is_escalated_by_faq_agent(self):
        tid = self._new_ticket()
        out = self._chat("Please cancel order #10001.", tid)

        self.assertEqual(self.router.engines_called(), [H.SENTIMENT_ENGINE, H.FAQ_ENGINE])
        self.assertEqual(out["ticket_status"], "ESCALATED")
        self.assertEqual(self.db.state(tid)[0], "ESCALATED")

    def test_pii_is_redacted_before_reaching_either_agent(self):
        tid = self._new_ticket()
        self._chat("My email is jane.doe@example.com, where is order #10101?", tid)

        for engine, _, body in self.router.calls:
            self.assertNotIn("jane.doe@example.com", json.dumps(body), engine)
        for llm in (self.sentiment_llm, self.faq_llm):
            for req in llm.requests:
                self.assertNotIn("jane.doe@example.com", H.request_text(req))

    def test_resolving_ticket_deletes_faq_agent_session(self):
        tid = self._new_ticket()
        self._chat("Where is order #10401?", tid)
        self.assertIsNotNone(self._faq_session(tid))

        r = self.client.post(f"/api/tickets/{tid}/resolve")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._faq_session(tid))
        self.assertIn((H.FAQ_ENGINE, "query"), [(e, meth) for e, meth, _ in self.router.calls])

    def test_faq_model_outage_falls_back_to_human_queue(self):
        self.faq_llm.error = RuntimeError("model unavailable")
        tid = self._new_ticket()
        out = self._chat("Where is order #10201?", tid)

        self.assertEqual(out["sentiment"], "NEUTRAL_FACTUAL")
        self.assertFalse(out["grounded"])
        self.assertEqual(out["ticket_status"], "ESCALATED")


def AR_ROUTE_FAQ():
    from backend.services import chat
    return chat.ROUTE_FAQ


if __name__ == "__main__":
    unittest.main()
