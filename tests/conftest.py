"""Shared pytest fixtures and test environment initialization."""
import os
import shutil
import sys
import tempfile
from unittest.mock import AsyncMock

import pytest

# Ensure repository root and test helpers sub-module are on sys.path
_TESTS_DIR = os.path.dirname(__file__)
_WORKSPACE_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_HELPERS_DIR = os.path.join(_TESTS_DIR, "helpers")

for _path in (_WORKSPACE_ROOT, _HELPERS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

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


def pytest_unconfigure(config):  # noqa: ARG001 - pytest hook signature
    if _TEST_DB_DIR:
        shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)



@pytest.fixture(autouse=True)
def _no_live_agent_session_cleanup(monkeypatch):
    """Resolving a ticket deletes the FAQ agent session over the network.

    Stub it for every test so the suite never calls the live Agent Runtime;
    tests that exercise the real function keep their own reference to it.
    """
    from backend.clients import agent_runtime

    monkeypatch.setattr(agent_runtime, "delete_faq_session", AsyncMock(return_value=True))
