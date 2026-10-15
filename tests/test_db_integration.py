import os
import sys
import uuid
import unittest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.main import app, rag_circuit_breaker

class TestDatabaseSessionLifecycleIntegration(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)
        rag_circuit_breaker.record_success()

    @patch("backend.main.call_faq_agent")
    @patch("backend.main.call_sentiment_agent")
    def test_session_id_mapping_escalation_and_resolution(self, mock_sentiment, mock_faq):
        """
        Comprehensive test for:
        1. Mapping session_id directly to ticket.id in Cloud SQL.
        2. Evaluating sentiment on initial query (NEW vs ESCALATED).
        3. Escalating status when a subsequent query in the session is URGENT_FRUSTRATED.
        4. Marking ticket as RESOLVED via /api/tickets/{ticket_id}/resolve.
        5. Rejecting new messages on RESOLVED tickets.
        """
        session_id = f"session_test_{uuid.uuid4().hex[:10]}"
        
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

        # 4. Attempting query on RESOLVED ticket returns 400
        resp_blocked = self.client.post("/api/chat", json={
            "message": "Can I add one more question?",
            "session_id": session_id,
            "customer_id": "cust_alex"
        })
        self.assertEqual(resp_blocked.status_code, 400)
        self.assertIn("RESOLVED", resp_blocked.json()["detail"])

if __name__ == "__main__":
    unittest.main()
