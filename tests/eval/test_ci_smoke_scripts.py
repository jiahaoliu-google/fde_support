"""Offline unit tests for Gate 4 and Gate 5 CI/CD smoke-test scripts."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scripts.ci import smoke_test_canary_e2e, smoke_test_deployed_agents


def test_smoke_deployed_agents_happy_path():
    """Gate 4 passes when both Sentiment Router and FAQ Assistant return expected responses."""
    with (
        patch(
            "scripts.ci.smoke_test_deployed_agents.agent_runtime.call_sentiment_agent",
            new=AsyncMock(return_value={"sentiment": "URGENT_FRUSTRATED", "confidence": 0.99}),
        ),
        patch(
            "scripts.ci.smoke_test_deployed_agents.agent_runtime.call_faq_agent",
            new=AsyncMock(
                return_value={
                    "answer": "Order #10001 is currently DELIVERED.",
                    "grounded": True,
                    "citations": ["Order #10001"],
                    "escalate": False,
                }
            ),
        ),
        patch(
            "scripts.ci.smoke_test_deployed_agents.agent_runtime.delete_faq_session",
            new=AsyncMock(return_value=True),
        ) as mock_del,
    ):
        exit_code = smoke_test_deployed_agents.main(
            [
                "--sentiment-url",
                "https://us-central1-aiplatform.googleapis.com/v1/sentiment:streamQuery",
                "--faq-url",
                "https://us-central1-aiplatform.googleapis.com/v1/faq:streamQuery",
            ]
        )
        assert exit_code == 0
        mock_del.assert_awaited_once()


def test_smoke_deployed_agents_fails_on_wrong_sentiment():
    """Gate 4 exits 1 (triggering rollback) if Sentiment Router misclassifies urgent probe."""
    with patch(
        "scripts.ci.smoke_test_deployed_agents.agent_runtime.call_sentiment_agent",
        new=AsyncMock(return_value={"sentiment": "NEUTRAL_INQUIRY", "confidence": 0.5}),
    ):
        assert smoke_test_deployed_agents.main([]) == 1


def test_smoke_deployed_agents_fails_on_ungrounded_faq_and_cleans_up():
    """Gate 4 exits 1 and cleans up the session if FAQ Assistant response is ungrounded."""
    with (
        patch(
            "scripts.ci.smoke_test_deployed_agents.agent_runtime.call_sentiment_agent",
            new=AsyncMock(return_value={"sentiment": "URGENT_FRUSTRATED", "confidence": 0.99}),
        ),
        patch(
            "scripts.ci.smoke_test_deployed_agents.agent_runtime.call_faq_agent",
            new=AsyncMock(
                return_value={
                    "answer": "",
                    "grounded": False,
                    "citations": [],
                    "escalate": True,
                }
            ),
        ),
        patch(
            "scripts.ci.smoke_test_deployed_agents.agent_runtime.delete_faq_session",
            new=AsyncMock(return_value=True),
        ) as mock_del,
    ):
        assert smoke_test_deployed_agents.main([]) == 1
        mock_del.assert_awaited_once()


def test_smoke_deployed_agents_handles_runtime_exception():
    """Gate 4 exits 1 if an unexpected exception occurs during the probe."""
    with patch(
        "scripts.ci.smoke_test_deployed_agents.agent_runtime.call_sentiment_agent",
        new=AsyncMock(side_effect=RuntimeError("Vertex endpoint unreachable")),
    ):
        assert smoke_test_deployed_agents.main([]) == 1


def test_fetch_oidc_id_token_env_and_fallbacks(monkeypatch):
    """Verify OIDC token resolution via env var, google-auth, IAM generateIdToken, and gcloud fallback."""
    monkeypatch.setenv("CI_CLOUD_RUN_ID_TOKEN", "env-token-123")
    assert smoke_test_canary_e2e.fetch_oidc_id_token("https://canary---backend.a.run.app") == "env-token-123"

    monkeypatch.delenv("CI_CLOUD_RUN_ID_TOKEN", raising=False)
    with patch("google.oauth2.id_token.fetch_id_token", return_value="google-oauth-token"):
        assert (
            smoke_test_canary_e2e.fetch_oidc_id_token("https://canary---backend.a.run.app")
            == "google-oauth-token"
        )

    with (
        patch("google.oauth2.id_token.fetch_id_token", side_effect=RuntimeError("no metadata server")),
        patch("google.auth.default", side_effect=RuntimeError("no ADC")),
        patch("subprocess.check_output", return_value=b"gcloud-cli-token\n"),
    ):
        assert (
            smoke_test_canary_e2e.fetch_oidc_id_token("https://canary---backend.a.run.app")
            == "gcloud-cli-token"
        )

    with (
        patch("google.oauth2.id_token.fetch_id_token", side_effect=RuntimeError("no metadata server")),
        patch("google.auth.default", side_effect=RuntimeError("no ADC")),
        patch("subprocess.check_output", side_effect=RuntimeError("gcloud failed")),
    ):
        with pytest.raises(RuntimeError, match="Unable to acquire OIDC ID token"):
            smoke_test_canary_e2e.fetch_oidc_id_token("https://canary---backend.a.run.app")


def _build_mock_canary_handler(
    *,
    frontend_health_status: int = 200,
    frontend_tickets_status: int = 401,
    backend_health_status: int = 200,
    customer_msg_text: str = "My email is [EMAIL_REDACTED], what is the status of order #10002?",
    urgent_routed_to: str = "Human Escalation Queue",
):
    """Builds a deterministic httpx.MockTransport handler for the 5 canary checks."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://canary---frontend.run.app/health":
            return httpx.Response(frontend_health_status, json={"status": "healthy"})
        if url == "https://canary---frontend.run.app/api/tickets":
            return httpx.Response(frontend_tickets_status, json={"detail": "Missing IAP JWT assertion."})
        if url == "https://canary---backend.run.app/health":
            return httpx.Response(
                backend_health_status,
                json={"status": "healthy", "database": "connected"},
            )
        if url == "https://canary---backend.run.app/api/chat" and request.method == "POST":
            import json

            body = json.loads(request.content.decode("utf-8"))
            if "10002" in body.get("message", ""):
                return httpx.Response(
                    200,
                    json={
                        "ticket_id": "t-neutral-1",
                        "sentiment": "NEUTRAL_FACTUAL",
                        "routed_to": "Automated RAG Assistant",
                        "ticket_status": "IN_PROGRESS",
                        "grounded": True,
                        "answer": "Order #10002 is PROCESSING.",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "ticket_id": "t-urgent-1",
                    "sentiment": "URGENT_FRUSTRATED",
                    "routed_to": urgent_routed_to,
                    "ticket_status": "ESCALATED",
                    "grounded": False,
                    "answer": "Escalated to specialist.",
                },
            )
        if url == "https://canary---backend.run.app/api/tickets/t-neutral-1":
            return httpx.Response(
                200,
                json={
                    "id": "t-neutral-1",
                    "status": "IN_PROGRESS",
                    "messages": [
                        {"sender": "CUSTOMER", "message_text": customer_msg_text},
                        {"sender": "AI_AGENT", "message_text": "Order #10002 is PROCESSING."},
                    ],
                },
            )
        if url.endswith("/resolve") and request.method == "POST":
            return httpx.Response(200, json={"status": "RESOLVED"})
        return httpx.Response(404, json={"detail": "Not found"})

    return httpx.MockTransport(handler)


def test_smoke_canary_e2e_happy_path(monkeypatch):
    """Gate 5 exits 0 when all 5 canary checks pass."""
    monkeypatch.setenv("CI_CLOUD_RUN_ID_TOKEN", "test-oidc-token")
    transport = _build_mock_canary_handler()
    real_client = httpx.Client

    with patch(
        "scripts.ci.smoke_test_canary_e2e.httpx.Client",
        side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
    ):
        rc = smoke_test_canary_e2e.main(
            [
                "--backend-url",
                "https://canary---backend.run.app",
                "--frontend-url",
                "https://canary---frontend.run.app",
            ]
        )
        assert rc == 0


def test_smoke_canary_e2e_fails_on_unredacted_pii(monkeypatch):
    """Gate 5 exits 1 (triggering rollback) if customer email is not redacted in DB."""
    monkeypatch.setenv("CI_CLOUD_RUN_ID_TOKEN", "test-oidc-token")
    transport = _build_mock_canary_handler(
        customer_msg_text="My email is canary-ci@example.com, what is the status of order #10002?"
    )
    real_client = httpx.Client

    with patch(
        "scripts.ci.smoke_test_canary_e2e.httpx.Client",
        side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
    ):
        assert (
            smoke_test_canary_e2e.run_canary_e2e_smoke(
                backend_url="https://canary---backend.run.app",
                frontend_url="https://canary---frontend.run.app",
            )
            is False
        )


def test_smoke_canary_e2e_fails_on_iap_guard_bypass(monkeypatch):
    """Gate 5 exits 1 if frontend /api/tickets does not return HTTP 401 without IAP JWT."""
    monkeypatch.setenv("CI_CLOUD_RUN_ID_TOKEN", "test-oidc-token")
    transport = _build_mock_canary_handler(frontend_tickets_status=200)
    real_client = httpx.Client

    with patch(
        "scripts.ci.smoke_test_canary_e2e.httpx.Client",
        side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
    ):
        assert (
            smoke_test_canary_e2e.run_canary_e2e_smoke(
                backend_url="https://canary---backend.run.app",
                frontend_url="https://canary---frontend.run.app",
            )
            is False
        )


def test_smoke_canary_e2e_fails_on_urgent_routing_error(monkeypatch):
    """Gate 5 exits 1 if urgent message does not route to HUMAN_ESCALATION."""
    monkeypatch.setenv("CI_CLOUD_RUN_ID_TOKEN", "test-oidc-token")
    transport = _build_mock_canary_handler(urgent_routed_to="FAQ_AGENT")
    real_client = httpx.Client

    with patch(
        "scripts.ci.smoke_test_canary_e2e.httpx.Client",
        side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
    ):
        assert (
            smoke_test_canary_e2e.run_canary_e2e_smoke(
                backend_url="https://canary---backend.run.app",
                frontend_url="https://canary---frontend.run.app",
            )
            is False
        )


def test_agent_fast_api_app_offline_boot_disables_cloud_telemetry(monkeypatch):
    """Gate 3 regression: fast_api_app disables cloud telemetry when OTEL_TRACES_EXPORTER=none so offline containers boot without GCP ADC."""
    import importlib.util
    from pathlib import Path
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    for agent_dir_name in ("faq_assistant", "sentiment_router"):
        agent_dir = repo_root / "agents" / agent_dir_name
        fast_api_path = agent_dir / "fast_api_app.py"

        mock_get_fast_api_app = MagicMock(return_value=MagicMock())
        mock_attach_routes = MagicMock()
        mock_services = MagicMock(
            ARTIFACT_SERVICE_URI="memory://",
            SESSION_SERVICE_URI="memory://",
        )

        with patch.dict(
            sys.modules,
            {
                "google.adk.cli.fast_api": MagicMock(get_fast_api_app=mock_get_fast_api_app),
                "app_utils": MagicMock(services=mock_services),
                "app_utils.services": mock_services,
                "app_utils.reasoning_engine_adapter": MagicMock(
                    attach_reasoning_engine_routes=mock_attach_routes
                ),
            },
        ):
            # 1. Offline Gate 3 container mode (OTEL_TRACES_EXPORTER=none)
            monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
            monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "false")
            spec = importlib.util.spec_from_file_location(
                f"test_{agent_dir_name}_fast_api_offline", fast_api_path
            )
            mod_offline = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(mod_offline)

            assert mod_offline._cloud_telemetry_enabled() is False
            _, kwargs_offline = mock_get_fast_api_app.call_args
            assert kwargs_offline["otel_to_cloud"] is False
            assert kwargs_offline["trace_to_cloud"] is False

            # 2. Production Agent Runtime mode (defaults enabled)
            mock_get_fast_api_app.reset_mock()
            monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
            monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
            monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "true")
            spec_prod = importlib.util.spec_from_file_location(
                f"test_{agent_dir_name}_fast_api_prod", fast_api_path
            )
            mod_prod = importlib.util.module_from_spec(spec_prod)
            assert spec_prod and spec_prod.loader
            spec_prod.loader.exec_module(mod_prod)

            assert mod_prod._cloud_telemetry_enabled() is True
            _, kwargs_prod = mock_get_fast_api_app.call_args
            assert kwargs_prod["otel_to_cloud"] is True
            assert kwargs_prod["trace_to_cloud"] is True


def test_agent_reasoning_engine_adapter_offline_without_adc(monkeypatch):
    """Gate 3 regression: reasoning_engine_adapter seeds global_config._project when ADC is absent."""
    import importlib.util
    from pathlib import Path
    import sys
    import types as pytypes

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from google.auth.exceptions import DefaultCredentialsError

    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "ci-test-project")

    class FakeGlobalConfig:
        def __init__(self):
            self._project = None

        @property
        def project(self):
            if not self._project:
                raise DefaultCredentialsError("No ADC in Gate 3 container")
            return self._project

    for agent_dir_name in ("faq_assistant", "sentiment_router"):
        adapter_path = (
            repo_root / "agents" / agent_dir_name / "app_utils" / "reasoning_engine_adapter.py"
        )
        fake_global_config = FakeGlobalConfig()
        fake_initializer = pytypes.SimpleNamespace(global_config=fake_global_config)
        fake_aiplatform = pytypes.ModuleType("google.cloud.aiplatform")
        fake_aiplatform.initializer = fake_initializer

        fake_services = pytypes.SimpleNamespace(
            get_session_service=lambda: None,
            get_artifact_service=lambda: None,
            ensure_session=MagicMock(),
            after_stream=MagicMock(),
        )
        fake_agent = pytypes.SimpleNamespace(
            app=pytypes.SimpleNamespace(name=f"{agent_dir_name}_app")
        )
        fake_pkg = pytypes.ModuleType("app_utils")
        fake_pkg.services = fake_services

        class FakeAdkApp:
            def __init__(self, **kwargs):
                # Mirrors vertexai.agent_engines.templates.adk.AdkApp.__init__ reading global_config.project
                assert fake_initializer.global_config.project == "ci-test-project"

            def set_up(self):
                pass

            def register_operations(self):
                return {"": ["list_sessions"], "stream": []}

            def list_sessions(self, **kwargs):
                return {"sessions": []}

        fake_adk_mod = pytypes.ModuleType("vertexai.agent_engines.templates.adk")
        fake_adk_mod.AdkApp = FakeAdkApp

        with patch.dict(
            sys.modules,
            {
                "google.cloud.aiplatform": fake_aiplatform,
                "google.cloud.aiplatform.initializer": fake_initializer,
                "vertexai": pytypes.ModuleType("vertexai"),
                "vertexai.agent_engines": pytypes.ModuleType("vertexai.agent_engines"),
                "vertexai.agent_engines.templates": pytypes.ModuleType(
                    "vertexai.agent_engines.templates"
                ),
                "vertexai.agent_engines.templates.adk": fake_adk_mod,
                "app_utils": fake_pkg,
                "app_utils.services": fake_services,
                "agent": fake_agent,
            },
        ):
            spec = importlib.util.spec_from_file_location(
                f"test_{agent_dir_name}_adapter_offline", adapter_path
            )
            adapter_mod = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(adapter_mod)

            app = FastAPI()
            adapter_mod.attach_reasoning_engine_routes(app)
            client = TestClient(app)
            resp = client.post(
                "/api/reasoning_engine",
                json={
                    "class_method": "list_sessions",
                    "input": {"user_id": "ci-boot-probe"},
                },
            )
            assert resp.status_code == 200
            assert resp.json() == {"output": {"sessions": []}}
            assert fake_global_config._project == "ci-test-project"


def test_canary_oidc_audience_strips_traffic_tag_prefix(monkeypatch):
    """Gate 5 regression: Cloud Run OIDC ID token audience must be the base service URL, not the canary--- tagged URL."""
    monkeypatch.delenv("CI_CLOUD_RUN_ID_TOKEN", raising=False)
    assert (
        smoke_test_canary_e2e.canonical_cloud_run_audience(
            "https://canary---frontend.run.app/"
        )
        == "https://frontend.run.app"
    )
    assert (
        smoke_test_canary_e2e.canonical_cloud_run_audience(
            "https://backend.run.app"
        )
        == "https://backend.run.app"
    )

    with patch(
        "google.oauth2.id_token.fetch_id_token",
        return_value="minted-oidc-jwt",
    ) as mock_fetch:
        tok = smoke_test_canary_e2e.fetch_oidc_id_token(
            "https://canary---backend.run.app"
        )
        assert tok == "minted-oidc-jwt"
        assert (
            mock_fetch.call_args[0][1]
            == "https://backend.run.app"
        )



