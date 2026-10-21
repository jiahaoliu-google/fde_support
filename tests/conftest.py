"""Shared pytest fixtures and test environment initialization."""
import os
import shutil
import sys
import tempfile
from unittest.mock import AsyncMock

import pytest

# Ensure repository root is on sys.path
_TESTS_DIR = os.path.dirname(__file__)
_WORKSPACE_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))

if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

# The backend picks its database at import time (backend.db.session). Outside production,
# with no DATABASE_URL, it fetches the Cloud SQL password via gcloud / Secret Manager and
# connects to the Auth Proxy on 127.0.0.1:5432. On a machine with gcloud logged in, a plain
# `pytest` then fails with "connection refused", or writes test data to the real Cloud SQL
# database if the proxy is running. So tests get a throw-away SQLite DB unless DATABASE_URL
# is set explicitly (e.g. to run against a dedicated Postgres on purpose).
# Must run before any `backend` import, which is why it lives at conftest import time.
_TEST_DB_DIR = None
if not os.environ.get("DATABASE_URL"):
    _TEST_DB_DIR = tempfile.mkdtemp(prefix="fde-pytest-db-")
    os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TEST_DB_DIR, 'test.db')}"

# Offline tests: never export spans to Cloud Trace (see backend/core/telemetry.py).
os.environ.setdefault("OTEL_TRACES_EXPORTER", "none")

# Offline tests: never call the real Cloud DLP API. It uses gRPC, which the no_network
# guard (tests/helpers/no_network.py) doesn't block. DLP behavior is tested with a fake
# client in tests/backend/test_model_armor_dlp.py.
os.environ.setdefault("MODEL_ARMOR_DLP_ENABLED", "false")
# Same for Model Armor (tests/backend/test_model_armor_api.py uses a fake client).
os.environ.setdefault("MODEL_ARMOR_ENABLED", "false")


def pytest_unconfigure(config):  # noqa: ARG001 - pytest hook signature
    if _TEST_DB_DIR:
        shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)



@pytest.fixture(autouse=True)
def _no_live_agent_session_cleanup(monkeypatch):
    """Resolving or listing tickets queries/deletes FAQ agent sessions over the network.

    Stub them for every test so the suite never calls the live Agent Runtime;
    tests that exercise the real functions keep their own references to them.
    """
    from backend.clients import agent_runtime

    monkeypatch.setattr(agent_runtime, "delete_faq_session", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_runtime, "list_faq_sessions", AsyncMock(return_value=None))


_GITIGNORED_METADATA_FILES = frozenset({
    os.path.abspath(os.path.join(_WORKSPACE_ROOT, "agents", "faq_assistant", "deployment_metadata.json")),
    os.path.abspath(os.path.join(_WORKSPACE_ROOT, "agents", "sentiment_router", "deployment_metadata.json")),
})


@pytest.fixture(autouse=True)
def _hide_gitignored_deployment_metadata(monkeypatch):
    """Simulate a clean CI checkout by hiding gitignored deployment_metadata.json files.

    Locally `agents/*/deployment_metadata.json` is left behind by `agents-cli deploy`,
    whereas in GitHub Actions (`actions/checkout@v4`) those gitignored files do not exist.
    Tests that exercise metadata fallback must point `_METADATA_FILE` at a temp fixture.
    """
    import builtins

    real_open = builtins.open

    def _guarded_open(file, *args, **kwargs):
        try:
            resolved = os.path.abspath(os.fspath(file))
        except TypeError:
            resolved = ""
        if resolved in _GITIGNORED_METADATA_FILES:
            raise FileNotFoundError(f"Gitignored in CI checkout: {resolved}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open)

