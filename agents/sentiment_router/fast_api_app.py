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

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    lifespan=lifespan,
    otel_to_cloud=True,
    trace_to_cloud=True,
)
app.title = "Sentiment Router Agent"
app.description = "API for interacting with Sentiment Router Agent"

# Attach Reasoning Engine routes so Vertex AI Console Playground works
attach_reasoning_engine_routes(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
