"""Tests for S1 (durable FAQ agent memory) and S2 (no unbounded agent session growth)."""
import asyncio
import importlib.util
import json
import os
import re
import sys
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

# The agents' images install google-adk[gcp] (which brings `vertexai`); the backend venv may not.
# These tests never make real Vertex calls, so a stub is enough for the import checks.
try:
    import vertexai  # noqa: F401
except ImportError:
    import types as _types
    for _name in ("vertexai", "vertexai.agent_engines", "vertexai.agent_engines.templates",
                  "vertexai.agent_engines.templates.adk"):
        sys.modules.setdefault(_name, _types.ModuleType(_name))
    sys.modules["vertexai.agent_engines.templates.adk"].AdkApp = object

import backend.main as main  # noqa: E402
from backend.main import app as backend_app, rag_circuit_breaker  # noqa: E402
from iap_test_utils import iap_headers  # noqa: E402

# Keep a reference to the real function; setUp() stubs the module attribute for endpoint tests.
REAL_DELETE_FAQ_SESSION = main.delete_faq_session

VERTEX_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
ALICE = "alice@example.com"
_ENV_KEYS = ("GOOGLE_CLOUD_AGENT_ENGINE_ID", "GOOGLE_API_CERTIFICATE_CONFIG", "REQUIRE_DURABLE_SESSIONS",
             "FAQ_SESSION_TTL_SECONDS")


def _load(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(coro):
    return asyncio.run(coro)


class _CleanEnv(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# FAQ agent: session store selection & TTL (S1)
# ---------------------------------------------------------------------------
class TestFaqSessionStore(_CleanEnv):
    def setUp(self):
        super().setUp()
        self.svc = _load("faq_services_under_test", "agents/faq_assistant/app_utils/services.py")

    def test_local_dev_falls_back_to_memory(self):
        from google.adk.sessions import InMemorySessionService
        service = self.svc.build_session_service()
        self.assertIsInstance(service, InMemorySessionService)

    def test_local_dev_ignores_repo_deployment_metadata(self):
        engine_id, _, _ = self.svc.resolve_agent_engine()
        self.assertIsNone(engine_id)

    def test_agent_runtime_uses_vertex(self):
        from google.adk.sessions import VertexAiSessionService
        os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ID"] = "6767283237606653952"
        service = self.svc.build_session_service()
        self.assertIsInstance(service, VertexAiSessionService)
        self.assertEqual(service._agent_engine_id, "6767283237606653952")
        self.assertEqual(service.ttl_seconds, 7 * 24 * 3600)

    def test_session_service_uri_full_resource_name(self):
        self.svc.SESSION_SERVICE_URI = (
            "agentengine://projects/p/locations/europe-west4/reasoningEngines/123"
        )
        engine_id, location, source = self.svc.resolve_agent_engine()
        self.assertEqual((engine_id, location, source), ("123", "europe-west4", "SESSION_SERVICE_URI"))

    def test_engine_location_never_global(self):
        """Agent Runtime sets GOOGLE_CLOUD_LOCATION=global; sessions are regional (prod 404 regression)."""
        with patch.dict(os.environ, {"GOOGLE_CLOUD_LOCATION": "global", "APP_URL": (
            "https://us-central1-aiplatform.googleapis.com/reasoningEngines/v1/projects/1/"
            "locations/asia-east1/reasoningEngines/2/api")}):
            self.assertEqual(self.svc.resolve_engine_location(), "asia-east1")
            self.assertEqual(self.svc.resolve_engine_location("global"), "asia-east1")
        with patch.dict(os.environ, {"GOOGLE_CLOUD_LOCATION": "global"}, clear=False):
            os.environ.pop("APP_URL", None)
            self.assertEqual(self.svc.resolve_engine_location(), "us-central1")
            os.environ["AGENT_ENGINE_LOCATION"] = "europe-west4"
            self.assertEqual(self.svc.resolve_engine_location(), "europe-west4")
            del os.environ["AGENT_ENGINE_LOCATION"]

    def test_metadata_fallback_only_on_agent_runtime(self):
        os.environ["GOOGLE_API_CERTIFICATE_CONFIG"] = "/var/run/cert_config.json"
        engine_id, location, source = self.svc.resolve_agent_engine()
        self.assertEqual(source, "deployment_metadata.json")
        self.assertTrue(engine_id.isdigit())
        self.assertEqual(location, "us-central1")

    def test_fails_closed_when_durable_required_and_no_engine(self):
        os.environ["REQUIRE_DURABLE_SESSIONS"] = "true"
        with self.assertRaises(RuntimeError):
            self.svc.build_session_service()

    def test_ttl_env_is_clamped(self):
        os.environ["FAQ_SESSION_TTL_SECONDS"] = "5"
        self.assertEqual(self.svc.session_ttl_seconds(), 60)
        os.environ["FAQ_SESSION_TTL_SECONDS"] = "garbage"
        self.assertEqual(self.svc.session_ttl_seconds(), 7 * 24 * 3600)

    def _vertex_service(self):
        os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ID"] = "123"
        return self.svc.build_session_service()

    def test_create_session_always_applies_ttl(self):
        from google.adk.sessions import VertexAiSessionService
        service = self._vertex_service()
        with patch.object(VertexAiSessionService, "create_session", new_callable=AsyncMock) as parent:
            run(service.create_session(app_name="a", user_id="u", session_id="t-1"))
        self.assertEqual(parent.call_args.kwargs["ttl"], f"{7 * 24 * 3600}s")

    def test_explicit_expiry_is_respected(self):
        from google.adk.sessions import VertexAiSessionService
        service = self._vertex_service()
        with patch.object(VertexAiSessionService, "create_session", new_callable=AsyncMock) as parent:
            run(service.create_session(app_name="a", user_id="u", session_id="t-1", ttl="60s"))
            run(service.create_session(app_name="a", user_id="u", session_id="t-2",
                                       expire_time="2030-01-01T00:00:00Z"))
        self.assertEqual(parent.call_args_list[0].kwargs["ttl"], "60s")
        self.assertNotIn("ttl", parent.call_args_list[1].kwargs)

    def test_memory_survives_process_restart(self):
        """Two independent service instances (= two processes) see the same conversation."""
        from google.adk.events import Event
        from google.adk.sessions import Session, VertexAiSessionService
        from google.genai import types

        backend_store: dict[str, Session] = {}  # stands in for the Vertex Sessions API

        async def fake_create(self, *, app_name, user_id, session_id=None, **kwargs):
            backend_store[session_id] = Session(app_name=app_name, user_id=user_id, id=session_id)
            return backend_store[session_id].model_copy(deep=True)

        async def fake_get(self, *, app_name, user_id, session_id, config=None):
            s = backend_store.get(session_id)
            return s.model_copy(deep=True) if s else None

        async def fake_append(self, session, event):
            backend_store[session.id].events.append(event)
            return event

        with patch.object(VertexAiSessionService, "create_session", fake_create), \
             patch.object(VertexAiSessionService, "get_session", fake_get), \
             patch.object(VertexAiSessionService, "append_event", fake_append):
            os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ID"] = "123"
            process_1 = self.svc.build_session_service()
            self.svc._session_service = process_1
            run(self.svc.ensure_session("faq_assistant_app", "u_1", "t-abc"))
            session = run(process_1.get_session(app_name="faq_assistant_app", user_id="u_1", session_id="t-abc"))
            event = Event(author="user", content=types.Content(role="user", parts=[types.Part(text="Order 10201")]))
            run(process_1.append_event(session, event))

            process_2 = self.svc.build_session_service()  # fresh instance, nothing in local memory
            restored = run(process_2.get_session(app_name="faq_assistant_app", user_id="u_1", session_id="t-abc"))
        self.assertEqual(restored.events[0].content.parts[0].text, "Order 10201")

    def test_ensure_session_is_idempotent(self):
        self.svc._session_service = None  # local dev -> in-memory
        run(self.svc.ensure_session("app", "u", "t-1"))
        run(self.svc.ensure_session("app", "u", "t-1"))
        sessions = run(self.svc.get_session_service().list_sessions(app_name="app", user_id="u")).sessions
        self.assertEqual(len(sessions), 1)


# ---------------------------------------------------------------------------
# Sentiment agent: per-request sessions are deleted (S2)
# ---------------------------------------------------------------------------
class TestSentimentSessionCleanup(unittest.TestCase):
    def setUp(self):
        self.svc = _load("sentiment_services_under_test", "agents/sentiment_router/app_utils/services.py")

    def test_no_sessions_left_after_many_requests(self):
        async def simulate():
            for _ in range(200):
                sid = f"s-{uuid.uuid4().hex}"
                await self.svc.ensure_session("sentiment_router_app", "sentiment_classifier", sid)
                await self.svc.after_stream("sentiment_router_app", "sentiment_classifier", sid)
        run(simulate())
        self.assertEqual(self.svc.get_session_service().session_count(), 0)

    def test_cap_evicts_oldest_when_cleanup_is_missed(self):
        service = self.svc.BoundedInMemorySessionService(max_sessions=5)

        async def leak():
            for i in range(12):
                await service.create_session(app_name="a", user_id="u", session_id=f"s-{i}")
        run(leak())
        self.assertEqual(service.session_count(), 5)
        remaining = {s.id for s in run(service.list_sessions(app_name="a", user_id="u")).sessions}
        self.assertEqual(remaining, {f"s-{i}" for i in range(7, 12)})

    def test_after_stream_never_raises(self):
        run(self.svc.after_stream("a", "u", "does-not-exist"))
        run(self.svc.after_stream("a", "u", None))


# ---------------------------------------------------------------------------
# Shared adapter: create-before-run, cleanup-after-run
# ---------------------------------------------------------------------------
class TestAdapterSessionHooks(unittest.TestCase):
    def test_adapters_are_identical(self):
        with open(os.path.join(ROOT, "agents/faq_assistant/app_utils/reasoning_engine_adapter.py")) as a, \
             open(os.path.join(ROOT, "agents/sentiment_router/app_utils/reasoning_engine_adapter.py")) as b:
            self.assertEqual(a.read(), b.read())

    def test_hooks_called_around_stream_even_on_failure(self):
        import types as pytypes
        from fastapi import FastAPI

        calls = []
        fake_services = pytypes.SimpleNamespace(
            get_session_service=lambda: None,
            get_artifact_service=lambda: None,
            ensure_session=AsyncMock(side_effect=lambda *a: calls.append(("ensure",) + a)),
            after_stream=AsyncMock(side_effect=lambda *a: calls.append(("after",) + a)),
        )

        class FakeAdkApp:
            def __init__(self, **kwargs):
                pass

            def set_up(self):
                pass

            def register_operations(self):
                return {"stream": ["stream_query"], "": []}

            def stream_query(self, **kwargs):
                yield {"ok": 1}
                raise RuntimeError("model blew up")

        fake_agent = pytypes.SimpleNamespace(app=pytypes.SimpleNamespace(name="sentiment_router_app"))
        fake_pkg = pytypes.ModuleType("app_utils")
        fake_pkg.services = fake_services
        with patch.dict(sys.modules, {"app_utils": fake_pkg, "app_utils.services": fake_services, "agent": fake_agent}):
            adapter = _load("adapter_under_test", "agents/sentiment_router/app_utils/reasoning_engine_adapter.py")
            adapter.AdkApp = FakeAdkApp
            app = FastAPI()
            adapter.attach_reasoning_engine_routes(app)
            client = TestClient(app, raise_server_exceptions=False)
            try:
                client.post("/stream_reasoning_engine", json={
                    "class_method": "stream_query",
                    "input": {"user_id": "sentiment_classifier", "session_id": "s-1", "message": "hi"},
                })
            except RuntimeError:
                pass
        self.assertEqual(calls[0], ("ensure", "sentiment_router_app", "sentiment_classifier", "s-1"))
        self.assertEqual(calls[-1], ("after", "sentiment_router_app", "sentiment_classifier", "s-1"))


# ---------------------------------------------------------------------------
# Backend: session ID mapping and cleanup on resolve
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _RecordingClient:
    """Minimal httpx.AsyncClient stand-in that records POSTs."""
    posts: list = []
    response = _FakeResp()
    error: Exception | None = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        if _RecordingClient.error:
            raise _RecordingClient.error
        _RecordingClient.posts.append({"url": url, "json": json})
        return _RecordingClient.response


class TestBackendAgentSessions(unittest.TestCase):
    def setUp(self):
        rag_circuit_breaker.record_success()
        _RecordingClient.posts = []
        _RecordingClient.response = _FakeResp()
        _RecordingClient.error = None
        self.patches = [
            patch.object(main.httpx, "AsyncClient", _RecordingClient),
            patch.object(main, "get_gcp_auth_headers", lambda: {}),
            patch.object(main, "delete_faq_session", AsyncMock(return_value=True)),
        ]
        for p in self.patches:
            p.start()
        self.alice = TestClient(backend_app, headers=iap_headers(ALICE))

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_agent_session_id_is_valid_for_vertex(self):
        for _ in range(1000):
            ticket = str(uuid.uuid4())
            sid = main.agent_session_id(ticket)
            self.assertRegex(sid, VERTEX_ID_RE)
            self.assertEqual(sid, main.agent_session_id(ticket))  # deterministic

    def test_agent_session_id_rejects_garbage(self):
        from fastapi import HTTPException
        for bad in ("../x", "a" * 80, "UPPER_case", ""):
            with self.assertRaises(HTTPException):
                main.agent_session_id(bad)

    def test_faq_payload_uses_mapped_session_id(self):
        ticket = str(uuid.uuid4())
        _RecordingClient.response = _FakeResp(200, json.dumps({"content": {"parts": [{"text": "hi"}]}}))
        run(main.call_faq_agent("hello", ticket, "u_abc"))
        self.assertEqual(_RecordingClient.posts[0]["json"]["input"]["session_id"], f"t-{ticket}")

    def test_faq_soft_fallbacks_escalate(self):
        # Both fallbacks tell the customer the question was routed to a human.
        for resp in (_FakeResp(200, ""), _FakeResp(500, "Model returned no actionable content")):
            _RecordingClient.response = resp
            data = run(main.call_faq_agent("hello", str(uuid.uuid4()), "u_abc"))
            self.assertTrue(data["escalate"])
            self.assertFalse(data["grounded"])

    def test_sentiment_session_id_is_vertex_safe(self):
        _RecordingClient.response = _FakeResp(200, json.dumps({"content": {"parts": [{"text": "{}"}]}}))
        run(main.call_sentiment_agent("hello"))
        self.assertRegex(_RecordingClient.posts[0]["json"]["input"]["session_id"], VERTEX_ID_RE)

    def test_delete_faq_session_payload(self):
        ticket = str(uuid.uuid4())
        self.assertTrue(run(REAL_DELETE_FAQ_SESSION(ticket, "u_abc")))
        post = _RecordingClient.posts[0]
        self.assertTrue(post["url"].endswith(":query"))
        self.assertEqual(post["json"], {
            "class_method": "async_delete_session",
            "input": {"user_id": "u_abc", "session_id": f"t-{ticket}"},
        })

    def test_delete_faq_session_never_raises(self):
        _RecordingClient.error = RuntimeError("network down")
        self.assertFalse(run(REAL_DELETE_FAQ_SESSION(str(uuid.uuid4()), "u")))
        _RecordingClient.error = None
        _RecordingClient.response = _FakeResp(500)
        self.assertFalse(run(REAL_DELETE_FAQ_SESSION(str(uuid.uuid4()), "u")))

    def test_resolve_deletes_agent_session_for_owner(self):
        ticket = self.alice.post("/api/tickets/create", json={}).json()["ticket_id"]
        resp = self.alice.post(f"/api/tickets/{ticket}/resolve")
        self.assertEqual(resp.status_code, 200)
        main.delete_faq_session.assert_awaited_once_with(ticket, main.agent_user_id(ALICE))

    def test_resolve_succeeds_when_cleanup_fails(self):
        main.delete_faq_session.side_effect = None
        main.delete_faq_session.return_value = False
        ticket = self.alice.post("/api/tickets/create", json={}).json()["ticket_id"]
        resp = self.alice.post(f"/api/tickets/{ticket}/resolve")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["current_status"], "RESOLVED")

    def test_foreign_resolve_does_not_touch_agent_session(self):
        ticket = self.alice.post("/api/tickets/create", json={}).json()["ticket_id"]
        bob = TestClient(backend_app, headers=iap_headers("bob@example.com"))
        self.assertEqual(bob.post(f"/api/tickets/{ticket}/resolve").status_code, 404)
        main.delete_faq_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
