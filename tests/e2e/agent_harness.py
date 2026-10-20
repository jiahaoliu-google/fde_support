"""In-process harness that runs both ADK agents behind the real backend.

Not a test module (no ``test_`` prefix). Used by tests/e2e/test_two_agent_e2e.py.

What is real:
  * backend: FastAPI app, IAP caller check, Model Armor redaction, ticket DB writes,
    circuit breaker, agent HTTP client (backend/clients/agent_runtime.py) and the
    NDJSON parsing (backend/clients/stream_parsing.py).
  * each agent: its ``agent.py`` (prompt, output schema, tools, after_model_callback),
    its ``app_utils/services.py`` session store and the shared
    ``app_utils/reasoning_engine_adapter.py`` routes that Agent Runtime calls.
  * the ADK Runner and FunctionTool execution, incl. the real ``get_order_status`` tool.

What is replaced:
  * the network hop backend -> Agent Runtime: an httpx transport routes each agent URL
    to that agent's FastAPI app in-process (``AgentRouter``).
  * ``vertexai``'s ``AdkApp`` wrapper: ``RunnerAdkApp`` does what ``AdkApp.stream_query``
    does (ADK ``Runner.run`` + ``event.model_dump_json(exclude_none=True)``) without the
    Vertex AI set-up and telemetry.
  * offline mode only: Gemini (``ScriptedSentimentLlm`` / ``ScriptedFaqLlm``) and the
    Discovery Engine search client (``FakeSearchClient``). Live mode keeps both real.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import sys
import types as pytypes
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.models import BaseLlm, LlmCapabilities, LlmRequest, LlmResponse
from google.genai import types
from pydantic import Field

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Fake Agent Runtime host: `.invalid` can never resolve, so a routing bug fails loudly
# instead of reaching a real endpoint.
AGENT_HOST = "agent-runtime.e2e.invalid"
SENTIMENT_ENGINE = "sentiment-router"
FAQ_ENGINE = "faq-assistant"
SENTIMENT_AGENT_URL = f"https://{AGENT_HOST}/v1/reasoningEngines/{SENTIMENT_ENGINE}:streamQuery"
FAQ_AGENT_URL = f"https://{AGENT_HOST}/v1/reasoningEngines/{FAQ_ENGINE}:streamQuery"

# Agent Runtime forwards :streamQuery / :query to these container routes.
_RUNTIME_ROUTES = {"streamQuery": "/api/stream_reasoning_engine", "query": "/api/reasoning_engine"}

_RealAsyncClient = httpx.AsyncClient


# ---------------------------------------------------------------------------
# Agent Runtime stand-ins
# ---------------------------------------------------------------------------
class RunnerAdkApp:
    """Replaces vertexai.agent_engines.templates.adk.AdkApp inside the adapter.

    Same constructor arguments, same operation names, and the same event JSON as
    AdkApp.stream_query, which runs ``Runner.run`` and yields
    ``json.loads(event.model_dump_json(exclude_none=True))`` per event.
    """

    def __init__(self, *, app, session_service_builder, artifact_service_builder, **_ignored):
        self._app = app
        self._session_service_builder = session_service_builder
        self._artifact_service_builder = artifact_service_builder
        self._runner = None
        self.session_service = None

    def set_up(self) -> None:
        from google.adk.runners import Runner

        self.session_service = self._session_service_builder()
        self._runner = Runner(
            app=self._app,
            session_service=self.session_service,
            artifact_service=self._artifact_service_builder(),
        )

    def register_operations(self) -> dict[str, list[str]]:
        return {"": [], "async": ["async_delete_session"], "stream": ["stream_query"], "async_stream": []}

    def stream_query(self, *, message: str, user_id: str, session_id: str | None = None, **_kwargs):
        content = types.Content(role="user", parts=[types.Part(text=message)])
        for event in self._runner.run(user_id=user_id, session_id=session_id, new_message=content):
            yield json.loads(event.model_dump_json(exclude_none=True))

    async def async_delete_session(self, *, user_id: str, session_id: str) -> None:
        await self.session_service.delete_session(app_name=self._app.name, user_id=user_id, session_id=session_id)


class AgentRouter(httpx.AsyncBaseTransport):
    """httpx transport: sends each Agent Runtime URL to that agent's FastAPI app in-process."""

    def __init__(self, apps: dict[str, FastAPI]):
        self._transports = {engine: httpx.ASGITransport(app=app) for engine, app in apps.items()}
        self.calls: list[tuple[str, str, dict]] = []  # (engine, method, request body)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != AGENT_HOST:
            raise AssertionError(f"Unexpected outbound agent call to {request.url}")
        engine, _, method = request.url.path.rsplit("/", 1)[-1].partition(":")
        if engine not in self._transports or method not in _RUNTIME_ROUTES:
            raise AssertionError(f"No agent serves {request.url}")
        body = await request.aread()
        self.calls.append((engine, method, json.loads(body)))
        forwarded = httpx.Request(
            request.method, f"http://{engine}{_RUNTIME_ROUTES[method]}",
            headers=request.headers, content=body,
        )
        return await self._transports[engine].handle_async_request(forwarded)

    def engines_called(self) -> list[str]:
        return [engine for engine, _, _ in self.calls]


def httpx_via(router: AgentRouter) -> pytypes.SimpleNamespace:
    """A stand-in for the `httpx` module as seen by backend.clients.agent_runtime only.

    Patching httpx.AsyncClient globally would also catch google-genai's own calls in live mode.
    """
    module = pytypes.SimpleNamespace(**{k: v for k, v in vars(httpx).items() if not k.startswith("__")})
    module.AsyncClient = lambda *args, **kwargs: _RealAsyncClient(*args, transport=router, **kwargs)
    return module


# ---------------------------------------------------------------------------
# Loading the agents exactly as their containers do
# ---------------------------------------------------------------------------
@dataclass
class AgentServer:
    engine: str
    agent: Any            # the ADK Agent object from agent.py
    services: Any         # the agent's app_utils/services.py module
    app: FastAPI          # routes from app_utils/reasoning_engine_adapter.py
    agent_app_name: str   # ADK App name, the session store's app_name


def _load_file(module_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(ROOT, rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _temporary_modules(mapping: dict[str, Any]):
    """Sets only the given sys.modules keys and restores them afterwards.

    (patch.dict(sys.modules) would also drop every module imported meanwhile.)
    """
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in mapping}
    sys.modules.update(mapping)
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _vertexai_stubs() -> dict[str, Any]:
    """The adapter imports AdkApp from vertexai; stub it if aiplatform isn't installed."""
    try:
        importlib.import_module("vertexai.agent_engines.templates.adk")
        return {}
    except ImportError:
        stubs = {}
        for name in ("vertexai", "vertexai.agent_engines", "vertexai.agent_engines.templates",
                     "vertexai.agent_engines.templates.adk"):
            stubs[name] = pytypes.ModuleType(name)
        stubs["vertexai.agent_engines.templates.adk"].AdkApp = RunnerAdkApp
        return stubs


def _serve_agent(agent_dir: str, engine: str, agent_attr: str) -> AgentServer:
    agent_module = importlib.import_module(f"agents.{agent_dir}.agent")
    services = _load_file(f"e2e_{agent_dir}_services", f"agents/{agent_dir}/app_utils/services.py")
    app_utils = pytypes.ModuleType("app_utils")
    app_utils.__path__ = []
    app_utils.services = services

    # In the container, `agent` and `app_utils` are top-level modules of the agent directory.
    with _temporary_modules({"agent": agent_module, "app_utils": app_utils,
                             "app_utils.services": services, **_vertexai_stubs()}):
        adapter = _load_file(f"e2e_{agent_dir}_adapter", f"agents/{agent_dir}/app_utils/reasoning_engine_adapter.py")
        adapter.AdkApp = RunnerAdkApp
        app = FastAPI(title=f"{agent_dir} (e2e)")
        adapter.attach_reasoning_engine_routes(app)
        # The adapter builds its runtime on the first request (`from agent import app`).
        # Trigger that now, while `agent` still resolves to this agent's module.
        warm_up = TestClient(app).post("/api/reasoning_engine", json={"class_method": "e2e_warm_up"})
        assert warm_up.status_code == 404, warm_up.text

    return AgentServer(engine=engine, agent=getattr(agent_module, agent_attr), services=services, app=app,
                       agent_app_name=agent_module.app.name)


_SERVERS: dict[str, AgentServer] | None = None


def agent_servers() -> dict[str, AgentServer]:
    """Both agents, loaded once per test process."""
    global _SERVERS
    if _SERVERS is None:
        _SERVERS = {
            SENTIMENT_ENGINE: _serve_agent("sentiment_router", SENTIMENT_ENGINE, "sentiment_router_agent"),
            FAQ_ENGINE: _serve_agent("faq_assistant", FAQ_ENGINE, "faq_assistant_agent"),
        }
    return _SERVERS


# ---------------------------------------------------------------------------
# Scripted Gemini stand-ins (offline mode)
# ---------------------------------------------------------------------------
_ORDER_ID_RE = re.compile(r"(?:#|\border\s*#?\s*|\bORD[-\s]?)?\b(\d{5})\b", re.IGNORECASE)
_URGENT_RE = re.compile(
    r"unacceptable|ridiculous|disappointed|useless|fed up|charged twice|nobody (?:answers|replied)|"
    r"cancel my account|!!!",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(r"\b(?:cancel|change (?:the|my) (?:shipping )?address|refund me)\b", re.IGNORECASE)
_ORDER_WORDS_RE = re.compile(r"\b(?:order|package|parcel|delivery)\b", re.IGNORECASE)
_POLICY_RE = re.compile(r"\b(?:return|warranty|refund policy|exchange)\b", re.IGNORECASE)


def _turn_view(llm_request: LlmRequest) -> tuple[str, list[types.FunctionResponse]]:
    """(latest customer text, tool results produced after it in this turn)."""
    tool_results: list[types.FunctionResponse] = []
    for content in reversed(llm_request.contents or []):
        parts = content.parts or []
        tool_results.extend(p.function_response for p in parts if p.function_response)
        texts = [p.text for p in parts if p.text and not p.thought]
        if content.role == "user" and texts:
            return " ".join(texts), list(reversed(tool_results))
    return "", list(reversed(tool_results))


def request_text(llm_request: LlmRequest) -> str:
    """All text the model was shown in one request (history included)."""
    return "\n".join(
        p.text for c in (llm_request.contents or []) for p in (c.parts or []) if p.text
    )


def _text_response(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def _tool_call(name: str, **args) -> LlmResponse:
    return LlmResponse(content=types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]))


class _ScriptedLlm(BaseLlm):
    """Deterministic model; records every request so tests can inspect what the agent sent."""

    requests: list = Field(default_factory=list, exclude=True)
    error: Exception | None = Field(default=None, exclude=True)  # set to simulate a model outage

    @property
    def capabilities(self) -> LlmCapabilities:
        # Same as Gemini on Vertex AI (production): output_schema together with tools.
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request)
        if self.error is not None:
            raise self.error
        yield self.respond(llm_request)

    def respond(self, llm_request: LlmRequest) -> LlmResponse:  # pragma: no cover - abstract
        raise NotImplementedError


class ScriptedSentimentLlm(_ScriptedLlm):
    """Plays gemini-2.5-flash for the Sentiment Router: label + entity extraction as JSON."""

    model: str = "scripted-sentiment"

    def respond(self, llm_request: LlmRequest) -> LlmResponse:
        text, _ = _turn_view(llm_request)
        order = _ORDER_ID_RE.search(text)
        return _text_response(json.dumps({
            "sentiment": "URGENT_FRUSTRATED" if _URGENT_RE.search(text) else "NEUTRAL_FACTUAL",
            "order_id": order.group(1) if order else None,
            "product_name": None,
            "email": None,
        }))


class ScriptedFaqLlm(_ScriptedLlm):
    """Plays gemini-2.5-pro for the FAQ Assistant, following the agent prompt's steps:
    ask for missing context, call one tool, then answer only from the tool result."""

    model: str = "scripted-faq"

    def respond(self, llm_request: LlmRequest) -> LlmResponse:
        text, tool_results = _turn_view(llm_request)
        if tool_results:
            return _text_response(json.dumps(self._answer_from(tool_results[-1])))
        if _ACTION_RE.search(text):
            return _text_response(json.dumps({
                "answer": "I can't make changes to orders myself, but a human agent can help you with that.",
                "citations": [], "grounded": False, "escalate": True,
            }))
        order = _ORDER_ID_RE.search(text)
        if order:
            return _tool_call("get_order_status", order_id=f"#{order.group(1)}")
        if _ORDER_WORDS_RE.search(text):
            return _text_response(json.dumps({
                "answer": "I can check that for you. What is your order ID? It looks like #12345.",
                "citations": [], "grounded": False, "escalate": False,
            }))
        if _POLICY_RE.search(text):
            return _tool_call("search_support_records", query=text)
        return _text_response(json.dumps({
            "answer": "Could you tell me which product or service this is about?",
            "citations": [], "grounded": False, "escalate": False,
        }))

    @staticmethod
    def _answer_from(result: types.FunctionResponse) -> dict:
        resp = result.response or {}
        if result.name == "get_order_status" and resp.get("status") == "found":
            o = resp["order"]
            return {
                "answer": (f"Your order {o['order_id']} is {o['order_status'].replace('_', ' ').lower()}. "
                           f"It is with {o['carrier']} (tracking {o['tracking_number']}) and is estimated "
                           f"to arrive on {o['estimated_delivery']}. {o['status_detail']}"),
                "citations": [f"Order {o['order_id']}"], "grounded": True, "escalate": False,
            }
        if result.name == "get_order_status":
            return {"answer": "I couldn't find that order. Could you double-check the order ID?",
                    "citations": [], "grounded": False, "escalate": False}
        if result.name == "search_support_records" and resp.get("status") == "success":
            top = resp["results"][0]
            # Leaks the doc ID into the answer on purpose: the agent's after_model_callback
            # must strip it before the customer sees it.
            return {"answer": f"{top['brand_response']} (doc_id: {top['doc_id']})",
                    "citations": [r["doc_id"] for r in resp["results"]], "grounded": True, "escalate": False}
        return {"answer": "I don't have specific information on that. Would you like a human agent to help?",
                "citations": [], "grounded": False, "escalate": True}


# ---------------------------------------------------------------------------
# Discovery Engine stand-in (offline mode)
# ---------------------------------------------------------------------------
SUPPORT_CORPUS = [
    {
        "doc_id": "twcs_400101",
        "keywords": ("return", "headphones"),
        "brand_author": "SoundCo",
        "customer_question": "How do I return headphones I bought last week?",
        "reference_brand_response_text": (
            "You can return SoundCo headphones within 30 days of delivery. Start an RMA from "
            "your order page and ship them back in the original packaging."
        ),
    },
    {
        "doc_id": "twcs_400102",
        "keywords": ("warranty", "headphones"),
        "brand_author": "SoundCo",
        "customer_question": "What does the headphone warranty cover?",
        "reference_brand_response_text": "SoundCo headphones carry a 1-year limited warranty against defects.",
    },
]


class FakeSearchClient:
    """Minimal SearchServiceClient: keyword match over SUPPORT_CORPUS, real SearchResult protos."""

    def __init__(self, queries: list[str]):
        self._queries = queries

    def search(self, request, timeout=None):
        from google.cloud import discoveryengine_v1 as discoveryengine

        self._queries.append(request.query)
        query = request.query.lower()
        hits = [d for d in SUPPORT_CORPUS if all(k in query for k in d["keywords"])]
        return [
            discoveryengine.SearchResponse.SearchResult(
                id=d["doc_id"],
                document=discoveryengine.Document(
                    id=d["doc_id"],
                    struct_data={k: v for k, v in d.items() if k != "keywords"},
                ),
            )
            for d in hits[: request.page_size or 5]
        ]
