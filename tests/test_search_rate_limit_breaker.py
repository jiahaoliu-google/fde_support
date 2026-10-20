"""C1: Agent Search rate limits (HTTP 429) must trip the circuit breaker.

The FAQ agent's search tool catches Discovery Engine errors and hands them to the
model, so the agent endpoint returns HTTP 200 even when search is rate-limited. These
tests check that the backend spots the failure in the agent's event stream and counts
it on the breaker.

The end-to-end test follows the TDD's ``test_circuit_breaker_behavior``: the Agent
Search client is mocked to raise 429, 3 queries are sent, and the 4th must bypass RAG
entirely and go to the human-queue fallback. The tool output in the stream comes from
the real ``search_support_records`` (only the Discovery Engine client is mocked), so
the test breaks if the tool's error contract changes.
"""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from google.api_core import exceptions as gax_exceptions

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import backend.main as main  # noqa: E402
from agents.faq_assistant import tools  # noqa: E402
from backend.circuit_breaker import CircuitState  # noqa: E402
from iap_test_utils import iap_headers  # noqa: E402

FAQ_URL = "https://example.test/v1/reasoningEngines/1:streamQuery"
NEUTRAL = {"sentiment": "NEUTRAL_FACTUAL", "order_id": None, "product_name": None, "email": None}
OUTAGE_FALLBACK_ROUTE = "Mock Human Queue Service (Circuit Breaker OPEN Fallback)"


def search_tool_output(side_effect=None, results=None) -> dict:
    """Runs the real search tool with a mocked Discovery Engine client."""
    client = MagicMock()
    if side_effect is not None:
        client.search.side_effect = side_effect
    else:
        client.search.return_value = iter(results or [])
    with patch.object(tools, "_get_search_client", return_value=client):
        return tools.search_support_records("What is your return policy for AirPods?")


def agent_stream(*tool_outputs: dict, answer: dict | None = None) -> str:
    """NDJSON the deployed FAQ agent streams for one turn: tool call(s), tool response(s), final answer."""
    events = []
    for out in tool_outputs:
        events.append({"content": {"role": "model", "parts": [
            {"function_call": {"name": "search_support_records", "args": {"query": "return policy AirPods"}}}]}})
        events.append({"content": {"role": "user", "parts": [
            {"function_response": {"name": "search_support_records", "response": out}}]}})
    answer = answer or {
        "answer": "Sorry, our system is temporarily unavailable. A human agent can help.",
        "citations": [], "grounded": False, "escalate": True,
    }
    events.append({"content": {"role": "model", "parts": [{"text": json.dumps(answer)}]}})
    return "\n".join(json.dumps(e) for e in events)


class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FaqAgentClient:
    """httpx.AsyncClient stand-in for the FAQ agent endpoint; records every POST."""
    posts: list = []
    response = _FakeResp()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FaqAgentClient.posts.append(url)
        return _FaqAgentClient.response


class TestDetectSearchToolFailure(unittest.TestCase):

    def test_rate_limited_is_429(self):
        raw = agent_stream(search_tool_output(gax_exceptions.ResourceExhausted("quota")))
        self.assertEqual(main.detect_search_tool_failure(raw), (429, "rate_limited"))

    def test_search_unavailable_is_503(self):
        raw = agent_stream(search_tool_output(gax_exceptions.ServiceUnavailable("down")))
        self.assertEqual(main.detect_search_tool_failure(raw), (503, "search_unavailable"))

    def test_config_errors_do_not_count(self):
        # Won't recover on their own; the agent's own escalate=true answer handles them.
        for exc in (gax_exceptions.Unauthenticated("x"), gax_exceptions.PermissionDenied("x")):
            raw = agent_stream(search_tool_output(exc))
            self.assertIsNone(main.detect_search_tool_failure(raw), type(exc).__name__)

    def test_healthy_search_is_not_a_failure(self):
        self.assertIsNone(main.detect_search_tool_failure(agent_stream(search_tool_output(results=[]))))

    def test_later_successful_search_clears_failure(self):
        raw = agent_stream({"status": "error", "error_type": "rate_limited", "results": []},
                           {"status": "no_results", "query": "q", "results": []})
        self.assertIsNone(main.detect_search_tool_failure(raw))

    def test_429_wins_over_503(self):
        raw = agent_stream({"status": "error", "error_type": "search_unavailable", "results": []},
                           {"status": "error", "error_type": "rate_limited", "results": []})
        self.assertEqual(main.detect_search_tool_failure(raw), (429, "rate_limited"))

    def test_no_tool_call_and_garbage_lines(self):
        self.assertIsNone(main.detect_search_tool_failure(""))
        self.assertIsNone(main.detect_search_tool_failure("not json\n[1, 2]\n{\"content\": \"x\"}"))
        self.assertIsNone(main.detect_search_tool_failure(json.dumps(
            {"content": {"parts": [{"function_response": {"name": "get_order_status",
                                                          "response": {"status": "error"}}}]}})))


class _FaqAgentTestBase(unittest.TestCase):

    def setUp(self):
        main.rag_circuit_breaker.record_success()
        main.rag_circuit_breaker.recovery_timeout_seconds = 30.0
        _FaqAgentClient.posts = []
        _FaqAgentClient.response = _FakeResp()
        self.patches = [
            patch.object(main.httpx, "AsyncClient", _FaqAgentClient),
            patch.object(main, "get_gcp_auth_headers", lambda: {}),
            patch.object(main, "FAQ_AGENT_URL", FAQ_URL),
            patch.object(main, "call_sentiment_agent", AsyncMock(return_value=NEUTRAL)),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(main.app, headers=iap_headers("alex@example.com"))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        main.rag_circuit_breaker.record_success()


class TestCallFaqAgentSurfacesSearchFailure(_FaqAgentTestBase):

    def test_raises_429_on_search_rate_limit(self):
        _FaqAgentClient.response = _FakeResp(200, agent_stream(
            search_tool_output(gax_exceptions.ResourceExhausted("quota"))))
        with self.assertRaises(main.UpstreamDependencyError) as ctx:
            asyncio.run(main.call_faq_agent("return policy?", "3f0c2b1e-0000-4000-8000-000000000001", "u_abc"))
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.reason, "rate_limited")

    def test_healthy_turn_still_returns_answer(self):
        answer = {"answer": "You can return AirPods within 14 days.", "citations": ["twcs_1"],
                  "grounded": True, "escalate": False}
        _FaqAgentClient.response = _FakeResp(200, agent_stream(
            {"status": "success", "query": "q", "results": [{"doc_id": "twcs_1"}]}, answer=answer))
        data = asyncio.run(main.call_faq_agent("return policy?", "3f0c2b1e-0000-4000-8000-000000000002", "u_abc"))
        self.assertTrue(data["grounded"])
        self.assertIn("14 days", data["answer"])


class TestCircuitBreakerBehaviorAgentSearch429(_FaqAgentTestBase):
    """TDD 'test_circuit_breaker_behavior', with the Agent Search client throwing HTTP 429."""

    def test_three_rate_limited_queries_open_breaker_and_fourth_bypasses_rag(self):
        _FaqAgentClient.response = _FakeResp(200, agent_stream(
            search_tool_output(gax_exceptions.ResourceExhausted("Quota exceeded"))))

        # Queries 1-3: each reaches the FAQ agent once (no retry on 429) and falls back.
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            for i in range(1, 4):
                self.assertEqual(main.rag_circuit_breaker.current_state, CircuitState.CLOSED, f"before query {i}")
                resp = self.client.post("/api/chat", json={"message": f"Query {i}: what is your return policy?"})
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertEqual(data["routed_to"], OUTAGE_FALLBACK_ROUTE)
                self.assertEqual(data["ticket_status"], "ESCALATED")
                self.assertIn("experiencing high volume", data["answer"])
                self.assertEqual(len(_FaqAgentClient.posts), i, "a 429 must not be retried")
        mock_sleep.assert_not_called()
        self.assertEqual(main.rag_circuit_breaker.state, CircuitState.OPEN)

        # Query 4: breaker OPEN, so RAG is bypassed entirely and the human queue gets it.
        resp = self.client.post("/api/chat", json={"message": "Query 4: do you ship to Germany?"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["routed_to"], OUTAGE_FALLBACK_ROUTE)
        self.assertEqual(data["citations"], ["Circuit Breaker OPEN ➔ Outage Fallback"])
        self.assertEqual(len(_FaqAgentClient.posts), 3, "query 4 must not reach the FAQ agent")

    def test_healthy_search_keeps_breaker_closed(self):
        answer = {"answer": "You can return AirPods within 14 days.", "citations": ["twcs_1"],
                  "grounded": True, "escalate": False}
        _FaqAgentClient.response = _FakeResp(200, agent_stream(
            {"status": "success", "query": "q", "results": [{"doc_id": "twcs_1"}]}, answer=answer))
        for i in range(4):
            resp = self.client.post("/api/chat", json={"message": f"Return policy for AirPods? ({i})"})
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("Circuit Breaker", resp.json()["routed_to"])
        self.assertEqual(main.rag_circuit_breaker.state, CircuitState.CLOSED)
        self.assertEqual(main.rag_circuit_breaker.failure_count, 0)

    def test_half_open_probe_rate_limited_reopens(self):
        for _ in range(3):
            main.rag_circuit_breaker.record_failure(429)
        main.rag_circuit_breaker.recovery_timeout_seconds = 0.0  # cooldown elapsed -> HALF_OPEN
        _FaqAgentClient.response = _FakeResp(200, agent_stream(
            search_tool_output(gax_exceptions.ResourceExhausted("Quota exceeded"))))

        resp = self.client.post("/api/chat", json={"message": "What is your return policy?"})
        self.assertEqual(resp.json()["routed_to"], OUTAGE_FALLBACK_ROUTE)
        self.assertEqual(len(_FaqAgentClient.posts), 1, "HALF_OPEN sends exactly one probe")
        self.assertEqual(main.rag_circuit_breaker.state, CircuitState.OPEN)


if __name__ == "__main__":
    unittest.main()
