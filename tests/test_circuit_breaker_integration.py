import os
import sys
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.main import app, rag_circuit_breaker
from iap_test_utils import iap_headers  # noqa: E402
from backend.circuit_breaker import CircuitState

class TestCircuitBreakerIntegration(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app, headers=iap_headers("alex@example.com"))
        # Reset circuit breaker before each test
        rag_circuit_breaker.record_success()
        rag_circuit_breaker.recovery_timeout_seconds = 1.0

    @patch("backend.main.call_sentiment_agent")
    @patch("backend.main.call_faq_agent")
    def test_circuit_breaker_trips_and_bypasses_rag(self, mock_call_faq, mock_call_sentiment):
        """
        Simulates Circuit Breaker behavior:
        1. Query 1 fails 3 times (retries), tripping circuit breaker to OPEN.
        2. Query 2 arrives while OPEN, bypasses downstream RAG entirely and routes to Mock Human Queue Service fallback.
        """
        mock_call_sentiment.return_value = {
            "sentiment": "NEUTRAL_FACTUAL",
            "order_id": None,
            "product_name": None,
            "email": None
        }
        mock_call_faq.side_effect = RuntimeError("Service Unavailable / Rate Limit Exceeded")

        # Query 1: Retries 3 times, all fail -> Circuit Breaker trips to OPEN, returns 200 with fallback
        with patch("asyncio.sleep", new_callable=AsyncMock):
            resp1 = self.client.post("/api/chat", json={"message": "Query 1: What is your refund policy?"})

        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertIn("Human Queue Service", data1["routed_to"])
        self.assertIn("human queue service", data1["answer"])
        self.assertEqual(mock_call_faq.call_count, 3)

        # Verify circuit breaker is now OPEN
        self.assertEqual(rag_circuit_breaker.state, CircuitState.OPEN)

        # Reset mock call count
        mock_call_faq.reset_mock()

        # Query 2: Circuit Breaker is OPEN -> Must BYPASS RAG call and return 200 with Fallback response
        resp2 = self.client.post("/api/chat", json={"message": "Query 2: What is your return policy?"})
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()

        self.assertEqual(data2["routed_to"], "Mock Human Queue Service (Circuit Breaker OPEN Fallback)")
        self.assertIn("experiencing high volume", data2["answer"])
        self.assertEqual(data2["citations"], ["Circuit Breaker OPEN ➔ Outage Fallback"])

        # Verify downstream FAQ agent function was NOT called during Query 2
        self.assertEqual(mock_call_faq.call_count, 0)

if __name__ == "__main__":
    unittest.main()
