import os
import sys
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.main import app

class TestDecoupledBackendAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health_endpoint(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["service"], "FDE Support Decoupled App Backend")

    def test_api_status_endpoint(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("backend_service", data)
        self.assertIn("agent_runtime_microservice", data)

    def test_empty_message_validation(self):
        response = self.client.post("/api/chat", json={"message": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Message cannot be empty")

    @patch("backend.main.call_sentiment_agent")
    def test_decoupled_urgent_human_escalation_route(self, mock_sentiment):
        """Urgent ticket sent over HTTP to Agent Runtime, classified and escalated."""
        mock_sentiment.return_value = {
            "sentiment": "URGENT_FRUSTRATED",
            "extracted_entities": {"order_id": "12345"}
        }
        urgent_payload = {
            "message": "URGENT EMERGENCY! I was charged twice for order #12345! My money was stolen and I am furious!"
        }
        response = self.client.post("/api/chat", json=urgent_payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["sentiment"], "URGENT_FRUSTRATED")
        self.assertEqual(data["routed_to"], "Human Escalation Queue")
        self.assertFalse(data["grounded"])
        self.assertIn("Order #12345", data["answer"])
        self.assertEqual(data["extracted_entities"].get("order_id"), "12345")

    @patch("backend.main.call_faq_agent")
    @patch("backend.main.call_sentiment_agent")
    def test_decoupled_neutral_rag_route(self, mock_sentiment, mock_faq):
        """Neutral query sent over HTTP to Agent Runtime, executing Vertex AI RAG search."""
        mock_sentiment.return_value = {
            "sentiment": "NEUTRAL_FACTUAL",
            "extracted_entities": {}
        }
        mock_faq.return_value = {
            "answer": "Electronic items can be returned within 14 days in original condition.",
            "citations": ["Electronic Return Policy"],
            "grounded": True
        }
        neutral_payload = {
            "message": "What is the return policy for opened electronic items?"
        }
        response = self.client.post("/api/chat", json=neutral_payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["sentiment"], "NEUTRAL_FACTUAL")
        self.assertEqual(data["routed_to"], "Automated RAG Assistant")
        self.assertIn("grounded", data)
        self.assertGreater(len(data["answer"]), 0)

if __name__ == "__main__":
    unittest.main()
