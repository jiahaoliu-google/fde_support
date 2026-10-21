import inspect
import json
import logging
import os
from fastapi import FastAPI, HTTPException, Request, responses
from vertexai.agent_engines.templates.adk import AdkApp
from app_utils import services

logger = logging.getLogger(__name__)

# Methods that run the agent against a caller-supplied session.
_SESSION_STREAM_METHODS = {"stream_query", "async_stream_query"}

def _no_op_instrumentor_builder(project_id: str) -> None:
    return None

def attach_reasoning_engine_routes(app: FastAPI) -> None:
    """Register reasoning_engine routes that dispatch to an AdkApp.

    This file is shared verbatim by every agent; per-agent session behaviour
    (durable vs. throw-away) lives in ``app_utils/services.py`` via the
    ``ensure_session`` / ``after_stream`` hooks.
    """
    runtime: AdkApp | None = None
    app_name: str = ""
    streaming_methods: set[str] = set()
    sync_methods: set[str] = set()

    def get_runtime() -> AdkApp:
        nonlocal runtime, app_name, streaming_methods, sync_methods
        if runtime is None:
            from agent import app as adk_app

            try:
                from google.cloud.aiplatform import initializer

                if not getattr(initializer.global_config, "_project", None):
                    try:
                        _ = initializer.global_config.project
                    except Exception:
                        initializer.global_config._project = os.environ.get(
                            "GOOGLE_CLOUD_PROJECT", "local-dev"
                        )
            except ImportError:
                pass

            app_name = adk_app.name
            runtime = AdkApp(
                app=adk_app,
                session_service_builder=services.get_session_service,
                artifact_service_builder=services.get_artifact_service,
                instrumentor_builder=_no_op_instrumentor_builder,
            )
            runtime.set_up()
            operations = runtime.register_operations()
            streaming_methods = set(operations.get("stream", [])) | set(
                operations.get("async_stream", [])
            )
            sync_methods = set(operations.get("", [])) | set(
                operations.get("async", [])
            )
        return runtime

    def resolve_method(class_method: str, *, streaming: bool):
        rt = get_runtime()
        allowed = streaming_methods if streaming else sync_methods
        if class_method not in allowed:
            raise HTTPException(
                status_code=404,
                detail=f"Unsupported reasoning_engine method: {class_method!r}",
            )
        return getattr(rt, class_method)

    @app.post("/api/stream_reasoning_engine")
    @app.post("/stream_reasoning_engine")
    async def stream_query(request: Request) -> responses.StreamingResponse:
        body = await request.json()
        class_method = body["class_method"]
        method = resolve_method(class_method, streaming=True)
        kwargs = body.get("input") or {}

        user_id = kwargs.get("user_id")
        session_id = kwargs.get("session_id")
        manages_session = class_method in _SESSION_STREAM_METHODS and bool(user_id) and bool(session_id)
        if manages_session:
            # Create-if-missing up front so behaviour doesn't depend on the
            # Runner's auto_create_session default (differs across ADK versions).
            try:
                await services.ensure_session(app_name, user_id, session_id)
            except ValueError as exc:  # invalid id / user mismatch
                raise HTTPException(status_code=400, detail="Invalid session") from exc

        async def generator():
            try:
                res = method(**kwargs)
                if hasattr(res, "__aiter__"):
                    async for event in res:
                        yield json.dumps(event) + "\n"
                else:
                    for event in res:
                        yield json.dumps(event) + "\n"
            finally:
                if manages_session:
                    await services.after_stream(app_name, user_id, session_id)

        return responses.StreamingResponse(
            content=generator(), media_type="application/json"
        )

    @app.post("/api/reasoning_engine")
    @app.post("/reasoning_engine")
    async def query(request: Request) -> responses.JSONResponse:
        body = await request.json()
        method = resolve_method(body["class_method"], streaming=False)
        kwargs = body.get("input") or {}
        output = (
            await method(**kwargs)
            if inspect.iscoroutinefunction(method)
            else method(**kwargs)
        )
        # Agent Runtime's :query contract is {"output": <result>}; anything else is reported
        # to the caller as HTTP 500 even though this container answered 200.
        # Pydantic results (e.g. list_sessions) aren't JSON-serialisable as-is.
        if hasattr(output, "model_dump"):
            output = output.model_dump(mode="json")
        return responses.JSONResponse(content={"output": output})
