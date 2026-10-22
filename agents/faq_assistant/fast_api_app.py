import os
import contextlib
from collections.abc import AsyncIterator
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from app_utils import services
from app_utils.reasoning_engine_adapter import attach_reasoning_engine_routes

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


def _cloud_telemetry_enabled() -> bool:
    if os.getenv("OTEL_TRACES_EXPORTER", "").strip().lower() == "none":
        return False
    if os.getenv("OTEL_SDK_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "true").strip().lower() in {
        "0",
        "false",
        "no",
    }:
        return False
    return True


_ENABLE_CLOUD_TELEMETRY = _cloud_telemetry_enabled()

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    lifespan=lifespan,
    otel_to_cloud=_ENABLE_CLOUD_TELEMETRY,
    trace_to_cloud=_ENABLE_CLOUD_TELEMETRY,
)
app.title = "FAQ Assistant Agent"
app.description = "API for interacting with FAQ Assistant Agent"

# Attach Reasoning Engine routes so Vertex AI Console Playground works
attach_reasoning_engine_routes(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
