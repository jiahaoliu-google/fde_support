"""Backend entrypoint: builds the FastAPI app and wires in the routers.

Run with `python -m backend.main` (the container CMD). Routes live in backend/api/,
the support-chat pipeline and ticket transactions in backend/services/, outbound
agent calls in backend/clients/, identity and PII redaction in backend/security/,
persistence in backend/db/, and settings/telemetry/circuit breaker in backend/core/.
"""
import os
import logging
import certifi
from fastapi import FastAPI

from backend.api import chat, health, tickets
from backend.core.config import settings
from backend.core.telemetry import instrument_app
from backend.db.session import init_db
from backend.security.model_armor import model_armor

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

logger = logging.getLogger("backend")
logging.basicConfig(level=logging.INFO if settings.DEBUG else logging.WARNING)

# Ensure database tables exist on module import / startup.
# Production fails fast: if the DB is unreachable the container exits, so Cloud Run never
# routes traffic to the revision (the previous healthy revision keeps serving).
try:
    init_db()
except Exception as e:
    if settings.is_production:
        logger.critical(f"Database initialization failed in production; refusing to start: {type(e).__name__}: {e}")
        raise
    logger.warning(f"Initial DB check warning: {e}")

app = FastAPI(
    title="FDE Support Decoupled App Backend",
    description="App Backend Server communicating with Agent Runtime Reasoning Engines.",
    version="2.5.0"
)

# No CORSMiddleware: the backend is only invoked server-to-server by the frontend proxy
# (Cloud Run IAM), never directly by browsers, so no cross-origin access is granted.

# OpenTelemetry Cloud Trace Instrumentation
instrument_app(app)

# Model Armor screening + PII redaction: create the Model Armor and Cloud DLP clients now
# (background, no API call) instead of on the first customer message. Logged at WARNING so
# it shows in production logs.
logger.warning(f"PII redaction / prompt screening: {model_armor.status()}")
model_armor.warm_up()

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(tickets.router)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
