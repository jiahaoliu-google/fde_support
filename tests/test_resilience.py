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

class TestHTTPServiceResilience(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app, headers=iap_headers("alex@example.com"))
        rag_circuit_breaker.record_success()

    def tearDown(self):
        rag_circuit_breaker.record_success()

    @patch("backend.main.call_sentiment_agent")
    def test_downstream_sentiment_agent_offline_escalates_to_human_queue(self, mock_sentiment):
        """
        Verifies resilience when Sentiment Agent raises an exception.
        Backend MUST catch the error, record failure on circuit breaker, and route to human queue service without crashing.
        """
        mock_sentiment.side_effect = RuntimeError("Sentiment Agent service unavailable")

        response = self.client.post("/api/chat", json={"message": "Where is my order #12345?"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["routed_to"], "Human Escalation Queue")
        self.assertEqual(data["sentiment"], "UNAVAILABLE")
        self.assertIn("human support queue service", data["answer"])

    @patch("backend.main.call_faq_agent")
    @patch("backend.main.call_sentiment_agent")
    def test_downstream_faq_agent_error_retries_and_escalates(self, mock_sentiment, mock_faq):
        """
        Verifies resilience when Sentiment agent succeeds but FAQ agent fails.
        Retries up to 3 times with exponential backoff (jittered), then escalates to the
        human queue service. The whole question counts as ONE circuit breaker failure.
        """
        mock_sentiment.return_value = {
            "sentiment": "NEUTRAL_FACTUAL",
            "order_id": None,
            "product_name": None,
            "email": None
        }
        mock_faq.side_effect = RuntimeError("Internal Agent Search Error")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            response = self.client.post("/api/chat", json={"message": "What is your return policy?"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("Human Queue Service", data["routed_to"])
        self.assertIn("human queue service", data["answer"])
        # Verify 3 call attempts were made before falling back
        self.assertEqual(mock_faq.call_count, 3)
        # Verify 2 backoff delays, each within its exponential cap (0.5 s, then 1.0 s)
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        self.assertEqual(len(delays), 2)
        self.assertTrue(0 <= delays[0] <= 0.5 and 0 <= delays[1] <= 1.0, delays)
        # One failed question = one breaker failure, so the breaker stays CLOSED
        self.assertEqual(rag_circuit_breaker.failure_count, 1)
        self.assertEqual(rag_circuit_breaker.state, CircuitState.CLOSED)

if __name__ == "__main__":
    unittest.main()
