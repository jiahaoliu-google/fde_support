"""Shared pytest fixtures."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _no_live_agent_session_cleanup(monkeypatch):
    """Resolving a ticket deletes the FAQ agent session over the network.

    Stub it for every test so the suite never calls the live Agent Runtime;
    tests that exercise the real function keep their own reference to it.
    """
    from backend.clients import agent_runtime

    monkeypatch.setattr(agent_runtime, "delete_faq_session", AsyncMock(return_value=True))
