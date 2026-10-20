"""Regression tests for SYSTEM_ANALYSIS.md 5.1 quick wins.

* #6  Sentiment output is schema-constrained, and the backend fails closed
      (escalates) on unparseable / unknown labels instead of defaulting to NEUTRAL.
* #7  GCP access tokens (backend) and Cloud Run ID tokens (frontend) are cached and
      refreshed only near expiry.
"""
import asyncio
import base64
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from backend.clients import agent_runtime  # noqa: E402

from backend.clients import gcp_auth  # noqa: E402
from backend.main import app as backend_app  # noqa: E402
from backend.clients.agent_runtime import rag_circuit_breaker  # noqa: E402
from iap_test_utils import iap_headers  # noqa: E402

ALICE = "alice@example.com"


def run(coro):
    return asyncio.run(coro)


def agent_stream(text: str) -> str:
    """One NDJSON line, as Agent Runtime's :streamQuery returns it."""
    return json.dumps({"content": {"parts": [{"text": text}]}})


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _Client:
    response = _Resp()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        return _Client.response


# ---------------------------------------------------------------------------
# #6 call_sentiment_agent: strict parsing
# ---------------------------------------------------------------------------
class TestSentimentParsing(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(agent_runtime.httpx, "AsyncClient", _Client),
            patch.object(gcp_auth, "get_gcp_auth_headers", lambda: {}),
            patch.object(agent_runtime, "SENTIMENT_AGENT_URL", "https://example.test/x:streamQuery"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _call(self, model_text):
        _Client.response = _Resp(200, agent_stream(model_text))
        return run(agent_runtime.call_sentiment_agent("msg"))

    def test_valid_labels_returned(self):
        for label in ("URGENT_FRUSTRATED", "NEUTRAL_FACTUAL"):
            self.assertEqual(self._call(json.dumps({"sentiment": label}))["sentiment"], label)

    def test_fenced_json_still_parsed(self):
        out = self._call('```json\n{"sentiment": "URGENT_FRUSTRATED", "order_id": "10201"}\n```')
        self.assertEqual(out["order_id"], "10201")

    def test_unparseable_output_raises_instead_of_neutral(self):
        for bad in ("I think this customer is upset", "", "{not json"):
            with self.assertRaises(agent_runtime.SentimentClassificationError, msg=bad):
                self._call(bad)

    def test_unknown_or_missing_label_raises(self):
        for bad in ('{"sentiment": "ANGRY"}', '{"sentiment": "urgent_frustrated"}', "{}", "[]", '"NEUTRAL_FACTUAL"'):
            with self.assertRaises(agent_runtime.SentimentClassificationError, msg=bad):
                self._call(bad)


# ---------------------------------------------------------------------------
# #6 /api/chat: fail closed
# ---------------------------------------------------------------------------
class TestSentimentFailClosed(unittest.TestCase):
    def setUp(self):
        rag_circuit_breaker.record_success()
        self.alice = TestClient(backend_app, headers=iap_headers(ALICE))
        self.faq = patch.object(agent_runtime, "call_faq_agent")
        self.mock_faq = self.faq.start()

    def tearDown(self):
        self.faq.stop()
        rag_circuit_breaker.record_success()

    def _chat(self, side_effect):
        with patch.object(agent_runtime, "call_sentiment_agent", side_effect=side_effect):
            return self.alice.post("/api/chat", json={"message": "Where is my order?"})

    def test_bad_classification_escalates_without_tripping_breaker(self):
        resp = self._chat(agent_runtime.SentimentClassificationError("garbage"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["ticket_status"], "ESCALATED")
        self.assertEqual(data["sentiment"], "UNAVAILABLE")
        self.assertEqual(data["routed_to"], "Human Escalation Queue")
        self.mock_faq.assert_not_called()
        self.assertEqual(rag_circuit_breaker.failure_count, 0)

    def test_outage_still_escalates_and_counts_for_breaker(self):
        resp = self._chat(RuntimeError("connection refused"))
        self.assertEqual(resp.json()["ticket_status"], "ESCALATED")
        self.mock_faq.assert_not_called()
        self.assertEqual(rag_circuit_breaker.failure_count, 1)


# ---------------------------------------------------------------------------
# #6 Agent-side schema
# ---------------------------------------------------------------------------
class TestSentimentAgentSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        saved = dict(os.environ)  # agent module sets Vertex/OTel env vars on import
        try:
            from agents.sentiment_router import agent as sentiment_agent
        finally:
            os.environ.clear()
            os.environ.update(saved)
        cls.mod = sentiment_agent

    def test_agent_uses_output_schema(self):
        self.assertIs(self.mod.sentiment_router_agent.output_schema, self.mod.SentimentResult)

    def test_schema_labels_match_backend(self):
        from typing import get_args
        labels = set(get_args(self.mod.SentimentResult.model_fields["sentiment"].annotation))
        self.assertEqual(labels, set(agent_runtime.SENTIMENT_LABELS))

    def test_schema_rejects_unknown_label(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self.mod.SentimentResult(sentiment="ANGRY")
        self.assertIsNone(self.mod.SentimentResult(sentiment="NEUTRAL_FACTUAL").order_id)


# ---------------------------------------------------------------------------
# #7 Backend access-token cache
# ---------------------------------------------------------------------------
class TestBackendCredentialCache(unittest.TestCase):
    def setUp(self):
        gcp_auth._gcp_credentials = None
        self.creds = MagicMock()
        self.creds.valid = False
        self.creds.token = "tok-1"

        def refresh(_req):
            self.creds.valid = True

        self.creds.refresh.side_effect = refresh
        self.default = patch("google.auth.default", return_value=(self.creds, "proj"))
        self.mock_default = self.default.start()

    def tearDown(self):
        self.default.stop()
        gcp_auth._gcp_credentials = None

    def test_loads_once_and_refreshes_only_when_invalid(self):
        for _ in range(5):
            self.assertEqual(gcp_auth.get_gcp_auth_headers(), {"Authorization": "Bearer tok-1"})
        self.mock_default.assert_called_once()
        self.creds.refresh.assert_called_once()

        self.creds.valid = False  # expired / inside google-auth's refresh margin
        gcp_auth.get_gcp_auth_headers()
        self.assertEqual(self.creds.refresh.call_count, 2)
        self.mock_default.assert_called_once()

    def test_failure_returns_empty_headers(self):
        self.creds.refresh.side_effect = RuntimeError("metadata down")
        self.assertEqual(gcp_auth.get_gcp_auth_headers(), {})


# ---------------------------------------------------------------------------
# #7 Frontend ID-token cache
# ---------------------------------------------------------------------------
def fake_jwt(exp: float) -> str:
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64({'exp': int(exp), 'aud': 'x'})}.c2ln"


class TestFrontendIdTokenCache(unittest.TestCase):
    def setUp(self):
        import frontend.server as server
        self.server = server
        server._id_token_cache.clear()
        self.issued = []

        def fetch(audience):
            tok = self.next_token()
            self.issued.append((audience, tok))
            return tok

        self.next_token = lambda: fake_jwt(time.time() + 3600)
        self.fetch = patch.object(server, "_fetch_id_token", side_effect=fetch)
        self.fetch.start()

    def tearDown(self):
        self.fetch.stop()
        self.server._id_token_cache.clear()

    def test_token_reused_until_near_expiry(self):
        first = self.server.get_id_token("https://backend")
        for _ in range(10):
            self.assertEqual(self.server.get_id_token("https://backend"), first)
        self.assertEqual(len(self.issued), 1)

    def test_refreshes_inside_margin(self):
        self.next_token = lambda: fake_jwt(time.time() + self.server.ID_TOKEN_REFRESH_MARGIN_SECONDS - 1)
        self.server.get_id_token("https://backend")
        self.server.get_id_token("https://backend")
        self.assertEqual(len(self.issued), 2)

    def test_failures_not_cached(self):
        self.next_token = lambda: ""
        self.assertEqual(self.server.get_id_token("https://backend"), "")
        self.next_token = lambda: fake_jwt(time.time() + 3600)
        self.assertTrue(self.server.get_id_token("https://backend"))
        self.assertEqual(len(self.issued), 2)

    def test_cache_is_per_audience(self):
        a = self.server.get_id_token("https://a")
        b = self.server.get_id_token("https://b")
        self.assertEqual([aud for aud, _ in self.issued], ["https://a", "https://b"])
        self.assertEqual(self.server.get_id_token("https://a"), a)
        self.assertEqual(self.server.get_id_token("https://b"), b)

    def test_unreadable_exp_uses_short_fallback_ttl(self):
        exp = self.server._token_expiry("not-a-jwt")
        ttl = exp - time.time() - self.server.ID_TOKEN_REFRESH_MARGIN_SECONDS
        self.assertAlmostEqual(ttl, self.server.ID_TOKEN_FALLBACK_TTL_SECONDS, delta=5)


if __name__ == "__main__":
    unittest.main()
