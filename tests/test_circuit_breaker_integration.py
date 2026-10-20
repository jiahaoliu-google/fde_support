"""Circuit breaker integration with /api/chat (TDD 'test_circuit_breaker_behavior').

The breaker records ONE outcome per customer question, however many retry attempts
that question took. So 3 consecutive failed questions open it, each of those 3 is
routed to the human queue, and the 4th question bypasses RAG entirely.
"""
import os
import sys
import unittest
from unittest.mock import patch, AsyncMock

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.clients import agent_runtime  # noqa: E402
from backend.main import app  # noqa: E402
from backend.clients.agent_runtime import rag_circuit_breaker  # noqa: E402
from iap_test_utils import iap_headers  # noqa: E402
from backend.core.circuit_breaker import CircuitState  # noqa: E402

NEUTRAL = {"sentiment": "NEUTRAL_FACTUAL", "order_id": None, "product_name": None, "email": None}
FALLBACK_ROUTE = "Mock Human Queue Service (Circuit Breaker OPEN Fallback)"
OK_ANSWER = {"answer": "Returns are accepted within 30 days.", "citations": ["twcs_1"], "grounded": True}


class _ChatTestBase(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app, headers=iap_headers("alex@example.com"))
        rag_circuit_breaker.record_success()
        rag_circuit_breaker.recovery_timeout_seconds = 30.0
        self.patches = [
            patch.object(agent_runtime, "call_sentiment_agent", AsyncMock(return_value=NEUTRAL)),
            patch.object(agent_runtime, "call_faq_agent", AsyncMock()),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ]
        _, self.mock_faq, self.mock_sleep = [p.start() for p in self.patches]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        rag_circuit_breaker.record_success()

    def ask(self, text="What is your refund policy?"):
        resp = self.client.post("/api/chat", json={"message": text})
        self.assertEqual(resp.status_code, 200)
        return resp.json()


class TestCircuitBreakerIntegration(_ChatTestBase):

    def test_three_failed_questions_open_breaker_and_fourth_bypasses_rag(self):
        self.mock_faq.side_effect = RuntimeError("Service Unavailable")

        # Questions 1-3: each is retried (3 attempts), then routed to the human queue.
        # Each question counts as ONE breaker failure, so it stays CLOSED until the 3rd.
        for i in range(1, 4):
            self.assertEqual(rag_circuit_breaker.current_state, CircuitState.CLOSED, f"before question {i}")
            data = self.ask(f"Question {i}: what is your refund policy?")
            self.assertEqual(data["routed_to"], FALLBACK_ROUTE)
            self.assertEqual(data["ticket_status"], "ESCALATED")
            self.assertIn("human queue service", data["answer"])
            self.assertEqual(self.mock_faq.call_count, 3 * i)
            self.assertEqual(rag_circuit_breaker.failure_count, i)
        self.assertEqual(rag_circuit_breaker.state, CircuitState.OPEN)

        # Question 4: OPEN, so the FAQ agent is never called.
        self.mock_faq.reset_mock()
        data = self.ask("Question 4: what is your return policy?")
        self.assertEqual(data["routed_to"], FALLBACK_ROUTE)
        self.assertIn("experiencing high volume", data["answer"])
        self.assertEqual(data["citations"], ["Circuit Breaker OPEN ➔ Outage Fallback"])
        self.mock_faq.assert_not_called()

    def test_one_failed_question_does_not_open_breaker(self):
        # Previously 3 failed attempts of a single question opened the breaker for everyone.
        self.mock_faq.side_effect = RuntimeError("Service Unavailable")
        self.ask()
        self.assertEqual(self.mock_faq.call_count, 3)
        self.assertEqual(rag_circuit_breaker.failure_count, 1)
        self.assertEqual(rag_circuit_breaker.state, CircuitState.CLOSED)

    def test_success_between_failures_resets_count(self):
        self.mock_faq.side_effect = [RuntimeError("x")] * 3 + [OK_ANSWER] + [RuntimeError("x")] * 6
        self.ask()                       # fails -> 1
        self.assertEqual(rag_circuit_breaker.failure_count, 1)
        self.ask()                       # succeeds -> reset
        self.assertEqual(rag_circuit_breaker.failure_count, 0)
        self.ask()
        self.ask()                       # 2 consecutive failures, still CLOSED
        self.assertEqual(rag_circuit_breaker.failure_count, 2)
        self.assertEqual(rag_circuit_breaker.state, CircuitState.CLOSED)

    def test_retry_that_recovers_answers_and_counts_no_failure(self):
        self.mock_faq.side_effect = [RuntimeError("blip"), OK_ANSWER]
        data = self.ask()
        self.assertNotIn("Circuit Breaker", data["routed_to"])
        self.assertIn("30 days", data["answer"])
        self.assertEqual(rag_circuit_breaker.failure_count, 0)


class TestRetryPolicy(_ChatTestBase):

    def test_backoff_is_exponential_with_jitter_and_bounded(self):
        self.mock_faq.side_effect = RuntimeError("503")
        with patch.object(agent_runtime.random, "uniform", side_effect=lambda lo, hi: hi) as mock_uniform:
            self.ask()
        # Full jitter: uniform(0, cap), cap doubling from the base delay.
        self.assertEqual([c.args for c in mock_uniform.call_args_list], [(0, 0.5), (0, 1.0)])
        self.assertEqual([c.args[0] for c in self.mock_sleep.call_args_list], [0.5, 1.0])
        self.assertLessEqual(agent_runtime.faq_retry_delay(10), agent_runtime.FAQ_RETRY_MAX_DELAY_SECONDS)

    def test_timeout_is_not_retried_and_counts_once(self):
        self.mock_faq.side_effect = httpx.ReadTimeout("agent hung")
        data = self.ask()
        self.assertEqual(data["routed_to"], FALLBACK_ROUTE)
        self.assertEqual(self.mock_faq.call_count, 1)
        self.mock_sleep.assert_not_called()
        self.assertEqual(rag_circuit_breaker.failure_count, 1)

    def test_rate_limit_is_not_retried(self):
        self.mock_faq.side_effect = HTTPException(status_code=429, detail="quota")
        self.ask()
        self.assertEqual(self.mock_faq.call_count, 1)
        self.mock_sleep.assert_not_called()
        self.assertEqual(rag_circuit_breaker.failure_count, 1)

    def test_client_error_is_not_retried_and_not_counted(self):
        # A 4xx won't change with more traffic: human fallback, but the breaker is untouched.
        self.mock_faq.side_effect = HTTPException(status_code=403, detail="forbidden")
        data = self.ask()
        self.assertEqual(data["routed_to"], FALLBACK_ROUTE)
        self.assertEqual(self.mock_faq.call_count, 1)
        self.assertEqual(rag_circuit_breaker.failure_count, 0)


class TestHalfOpenProbe(_ChatTestBase):

    def _open_then_cool_down(self):
        for _ in range(3):
            rag_circuit_breaker.record_failure(503)
        rag_circuit_breaker.recovery_timeout_seconds = 0.0

    def test_probe_success_closes_breaker(self):
        self._open_then_cool_down()
        self.mock_faq.return_value = OK_ANSWER
        data = self.ask()
        self.assertNotIn("Circuit Breaker", data["routed_to"])
        self.assertEqual(rag_circuit_breaker.state, CircuitState.CLOSED)

    def test_probe_is_single_attempt_and_failure_reopens(self):
        self._open_then_cool_down()
        self.mock_faq.side_effect = RuntimeError("still down")
        data = self.ask()
        self.assertEqual(data["routed_to"], FALLBACK_ROUTE)
        self.assertEqual(self.mock_faq.call_count, 1, "the HALF_OPEN probe is not retried")
        self.assertEqual(rag_circuit_breaker.state, CircuitState.OPEN)

    def test_requests_during_probe_take_fallback(self):
        self._open_then_cool_down()
        self.assertEqual(rag_circuit_breaker.try_acquire(), CircuitState.HALF_OPEN)  # probe in flight
        data = self.ask()
        self.assertEqual(data["routed_to"], FALLBACK_ROUTE)
        self.mock_faq.assert_not_called()

    def test_probe_slot_released_after_client_error(self):
        self._open_then_cool_down()
        self.mock_faq.side_effect = HTTPException(status_code=403, detail="forbidden")
        self.ask()
        self.assertEqual(rag_circuit_breaker.current_state, CircuitState.HALF_OPEN)
        self.assertFalse(rag_circuit_breaker.probe_in_flight)


if __name__ == "__main__":
    unittest.main()
