"""FDE Support app backend.

Layout:
  main.py    FastAPI app wiring (entrypoint: `python -m backend.main`)
  api/       HTTP layer: routers, request/response schemas, auth dependencies
  services/  Business logic: the support-chat pipeline and ticket transactions
  clients/   Outbound calls: Agent Runtime agents, Google auth, stream parsing
  security/  Caller identity (inbound) and PII redaction (outbound to the LLM)
  db/        SQLAlchemy engine, sessions and ORM models
  core/      Cross-cutting infrastructure: settings, telemetry, circuit breaker
"""
