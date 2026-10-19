"""Session / artifact services for the Sentiment Router agent.

The classifier is stateless: it never reads earlier turns. Each request gets a
throw-away in-memory session that is deleted as soon as the run finishes
(fixes S2). There are no extra network calls, and no copy of customer text is
kept after the request.

As a safety net, the store is capped at ``MAX_SESSIONS``; if any session slips
past deletion (e.g. the client disconnects mid-stream), the oldest are evicted.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from google.adk.artifacts import BaseArtifactService, InMemoryArtifactService
from google.adk.sessions import BaseSessionService, InMemorySessionService, Session

logger = logging.getLogger(__name__)

SESSION_SERVICE_URI = os.getenv("SESSION_SERVICE_URI", "memory://")
ARTIFACT_SERVICE_URI = os.getenv("ARTIFACT_SERVICE_URI", "memory://")

MAX_SESSIONS = 1000


class BoundedInMemorySessionService(InMemorySessionService):
    """InMemorySessionService that never holds more than ``max_sessions`` sessions."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        super().__init__()
        self.max_sessions = max_sessions

    def session_count(self) -> int:
        return sum(len(by_id) for users in self.sessions.values() for by_id in users.values())

    def _evict_oldest(self) -> None:
        overflow = self.session_count() - self.max_sessions + 1  # make room for the new one
        if overflow <= 0:
            return
        entries = [
            (s.last_update_time, app, uid, sid)
            for app, users in self.sessions.items()
            for uid, by_id in users.items()
            for sid, s in by_id.items()
        ]
        entries.sort()
        for _, app, uid, sid in entries[:overflow]:
            self.sessions[app][uid].pop(sid, None)
            if not self.sessions[app][uid]:
                del self.sessions[app][uid]
        logger.warning("Sentiment session store hit cap %d; evicted %d stale session(s).", self.max_sessions, overflow)

    async def create_session(self, **kwargs: Any) -> Session:
        self._evict_oldest()
        return await super().create_session(**kwargs)


_session_service = BoundedInMemorySessionService()
_artifact_service = InMemoryArtifactService()


def get_session_service() -> BaseSessionService:
    return _session_service


def get_artifact_service() -> BaseArtifactService:
    return _artifact_service


async def ensure_session(app_name: str, user_id: str, session_id: str) -> None:
    existing = await _session_service.get_session(app_name=app_name, user_id=user_id, session_id=session_id)
    if existing is None:
        await _session_service.create_session(app_name=app_name, user_id=user_id, session_id=session_id)


async def after_stream(app_name: str, user_id: str, session_id: Optional[str]) -> None:
    """Deletes the one-shot classification session once the run ends (success or failure)."""
    if not session_id:
        return
    try:
        await _session_service.delete_session(app_name=app_name, user_id=user_id, session_id=session_id)
    except Exception:  # never fail the request over cleanup; the cap is the backstop
        logger.warning("Failed to delete sentiment session.", exc_info=True)
