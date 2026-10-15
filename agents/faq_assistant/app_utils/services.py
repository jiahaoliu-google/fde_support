import os
from google.adk.sessions import InMemorySessionService, BaseSessionService
from google.adk.artifacts import InMemoryArtifactService, BaseArtifactService

SESSION_SERVICE_URI = os.getenv("SESSION_SERVICE_URI", "memory://")
ARTIFACT_SERVICE_URI = os.getenv("ARTIFACT_SERVICE_URI", "memory://")

_session_service = InMemorySessionService()
_artifact_service = InMemoryArtifactService()

def get_session_service() -> BaseSessionService:
    return _session_service

def get_artifact_service() -> BaseArtifactService:
    return _artifact_service
