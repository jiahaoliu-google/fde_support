import os
import sys
import unittest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


from backend.main import app

from backend.clients.agent_runtime import rag_circuit_breaker
from iap_test_utils import iap_headers  # noqa: E402

class TestDatabaseSessionLifecycleIntegration(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app, headers=iap_headers("alex@example.com"))
        rag_circuit_breaker.record_success()

    @patch("backend.clients.agent_runtime.delete_faq_session", new_callable=AsyncMock, return_value=True)
    @patch("backend.clients.agent_runtime.call_faq_agent")
    @patch("backend.clients.agent_runtime.call_sentiment_agent")
    def test_session_id_mapping_escalation_and_resolution(self, mock_sentiment, mock_faq, mock_delete_session):
        """
        Comprehensive test for:
        1. Mapping session_id directly to ticket.id in Cloud SQL.
        2. Evaluating sentiment on initial query (NEW vs ESCALATED).
        3. Escalating status when a subsequent query in the session is URGENT_FRUSTRATED.
        4. Marking ticket as RESOLVED via /api/tickets/{ticket_id}/resolve.
        5. Rejecting new messages on RESOLVED tickets.
        """
        create_resp = self.client.post("/api/tickets/create", json={})
        self.assertEqual(create_resp.status_code, 200)
        session_id = create_resp.json()["ticket_id"]
        self.assertEqual(create_resp.json()["customer_id"], "alex@example.com")
        
        # 1. Query 1: Neutral question -> creates ticket with NEW/IN_PROGRESS status
        mock_sentiment.return_value = {
            "sentiment": "NEUTRAL_FACTUAL",
            "extracted_entities": {}
        }
        mock_faq.return_value = {
            "answer": "Our return policy allows returns within 30 days.",
            "citations": ["FAQ Policy Doc"],
            "grounded": True
        }

        resp1 = self.client.post("/api/chat", json={
            "message": "What is the return policy?",
            "session_id": session_id,
            "customer_id": "cust_alex"
        })

        self.assertEqual(resp1.status_code, 200)
        d1 = resp1.json()
        self.assertEqual(d1["ticket_id"], session_id)
        self.assertEqual(d1["ticket_status"], "IN_PROGRESS")
        self.assertEqual(d1["sentiment"], "NEUTRAL_FACTUAL")

        # 2. Query 2: Urgent/Angry query in same session -> escalates ticket
        mock_sentiment.return_value = {
            "sentiment": "URGENT_FRUSTRATED",
            "extracted_entities": {"order_id": "554433"}
        }

        resp2 = self.client.post("/api/chat", json={
            "message": "I AM FURIOUS! ORDER #554433 IS MISSING AND I WANT MY MONEY BACK!",
            "session_id": session_id,
            "customer_id": "cust_alex"
        })

        self.assertEqual(resp2.status_code, 200)
        d2 = resp2.json()
        self.assertEqual(d2["ticket_id"], session_id)
        self.assertEqual(d2["ticket_status"], "ESCALATED")
        self.assertEqual(d2["sentiment"], "URGENT_FRUSTRATED")

        # 3. Resolve ticket via /api/tickets/{ticket_id}/resolve
        res_resp = self.client.post(f"/api/tickets/{session_id}/resolve")
        self.assertEqual(res_resp.status_code, 200)
        res_data = res_resp.json()
        self.assertEqual(res_data["current_status"], "RESOLVED")
        mock_delete_session.assert_awaited_once()
        self.assertEqual(mock_delete_session.await_args.args[0], session_id)

        # 4. Attempting query on RESOLVED ticket returns 400
        resp_blocked = self.client.post("/api/chat", json={
            "message": "Can I add one more question?",
            "session_id": session_id,
            "customer_id": "cust_alex"
        })
        self.assertEqual(resp_blocked.status_code, 400)
        self.assertIn("RESOLVED", resp_blocked.json()["detail"])

    def _new_ticket(self):
        return self.client.post("/api/tickets/create", json={}).json()["ticket_id"]

    @patch("backend.clients.agent_runtime.call_faq_agent")
    @patch("backend.clients.agent_runtime.call_sentiment_agent")
    def test_escalated_ticket_followups_bypass_agents(self, mock_sentiment, mock_faq):
        """Scoping rule: once ESCALATED, no sentiment/FAQ call; follow-ups are queued for the human."""
        ticket_id = self._new_ticket()
        mock_sentiment.return_value = {"sentiment": "URGENT_FRUSTRATED", "extracted_entities": {"order_id": "10201"}}
        r1 = self.client.post("/api/chat", json={"message": "I was CHARGED TWICE for order #10201!", "session_id": ticket_id})
        self.assertEqual(r1.json()["ticket_status"], "ESCALATED")
        mock_sentiment.reset_mock()

        r2 = self.client.post("/api/chat", json={"message": "Any update on that?", "session_id": ticket_id})
        self.assertEqual(r2.status_code, 200)
        d2 = r2.json()
        self.assertEqual(d2["ticket_status"], "ESCALATED")
        self.assertEqual(d2["routed_to"], "Human Escalation Queue")
        mock_sentiment.assert_not_called()
        mock_faq.assert_not_called()

        transcript = self.client.get(f"/api/tickets/{ticket_id}").json()["messages"]
        customer_turns = [m["message_text"] for m in transcript if m["sender"] == "CUSTOMER"]
        self.assertIn("Any update on that?", customer_turns)
        self.assertFalse(any(m["sender"] == "AGENT" for m in transcript))

    @patch("backend.clients.agent_runtime.call_faq_agent")
    @patch("backend.clients.agent_runtime.call_sentiment_agent")
    def test_faq_soft_fallback_escalates(self, mock_sentiment, mock_faq):
        """'Routed to our support team' must actually escalate, so the FAQ agent isn't used again."""
        ticket_id = self._new_ticket()
        mock_sentiment.return_value = {"sentiment": "NEUTRAL_FACTUAL", "extracted_entities": {}}
        mock_faq.return_value = {
            "answer": "I don't have specific information on that right now, so I've routed your question to our support team for further assistance.",
            "citations": ["FAQ Datastore Search"], "grounded": False, "escalate": True,
        }
        r = self.client.post("/api/chat", json={"message": "Obscure question", "session_id": ticket_id})
        self.assertEqual(r.json()["ticket_status"], "ESCALATED")

if __name__ == "__main__":
    unittest.main()
