"""OpenTelemetry / Cloud Trace setup for the backend.

`tracer` is created at import time; `instrument_app(app)` adds FastAPI and httpx
auto-instrumentation once the app object exists.

Set the standard OpenTelemetry variable OTEL_TRACES_EXPORTER=none to turn export off.
Then no global TracerProvider is installed and nothing is sent to Cloud Trace. The
offline tests and the pre-deployment agent eval do this: they import backend code
in-process, and a global Cloud Trace exporter would also export the ADK agents' spans,
which needs roles/cloudtrace.agent that the CI service account doesn't have.
"""
import logging
import os

logger = logging.getLogger("backend")

_instrumentation_enabled = False


def _export_disabled() -> bool:
    return os.getenv("OTEL_TRACES_EXPORTER", "").strip().lower() == "none"


try:
    if _export_disabled():
        raise RuntimeError("OTEL_TRACES_EXPORTER=none")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME

    resource = Resource.create({
        SERVICE_NAME: "fde-support-backend",
        "service.name": "fde-support-backend",
        "service.namespace": "fde-support",
        "service.version": "2.5.0"
    })

    tracer_provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(tracer_provider)

    # CRITICAL: Pass resource_regex=".*" so opentelemetry-exporter-gcp-trace exports custom resource attributes
    cloud_trace_exporter = CloudTraceSpanExporter(resource_regex=".*")
    tracer_provider.add_span_processor(BatchSpanProcessor(cloud_trace_exporter))

    tracer = trace.get_tracer("fde-support-backend")
    _instrumentation_enabled = True
except Exception as e:
    if _export_disabled():
        logger.info("Cloud Trace export disabled (OTEL_TRACES_EXPORTER=none).")
    else:
        logger.warning(f"OpenTelemetry Cloud Trace setup skipped or fallback: {e}")
    from opentelemetry import trace
    tracer = trace.get_tracer("fde-support-backend")


def instrument_app(app) -> None:
    """Adds FastAPI and httpx auto-instrumentation (no-op if tracing setup failed)."""
    if not _instrumentation_enabled:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
        logger.info("OpenTelemetry Cloud Trace instrumentation initialized with resource_regex='.*' and service.name=fde-support-backend.")
    except Exception as e:
        logger.warning(f"OpenTelemetry Cloud Trace setup skipped or fallback: {e}")
