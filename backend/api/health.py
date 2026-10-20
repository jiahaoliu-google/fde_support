"""Health, status and index routes."""
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from backend.clients import agent_runtime

logger = logging.getLogger("backend")

router = APIRouter()

@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "FDE Support Decoupled App Backend",
        "circuit_breaker_state": agent_runtime.rag_circuit_breaker.current_state.value,
        "database": "connected",
        "version": "2.5.0"
    }

@router.get("/api/status")
async def get_services_status():
    return {
        "backend_service": {"status": "online", "port": 8000},
        "circuit_breaker": {
            "state": agent_runtime.rag_circuit_breaker.current_state.value,
            "failure_count": agent_runtime.rag_circuit_breaker.failure_count
        },
        "database": {"status": "active"},
        "agent_runtime_microservice": {
            "status": "online",
            "faq_agent_url": agent_runtime.FAQ_AGENT_URL,
            "sentiment_agent_url": agent_runtime.SENTIMENT_AGENT_URL
        }
    }

@router.get("/", response_class=HTMLResponse)
async def get_index():
    return """<!DOCTYPE html>
<html>
<head><title>FDE Support Decoupled Backend API</title></head>
<body><h1>FDE Support Decoupled Backend Active with Cloud SQL Persistence</h1></body>
</html>"""
