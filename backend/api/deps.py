"""FastAPI dependencies: the authenticated caller and derived per-user keys."""
import hashlib
import logging
import re

from fastapi import HTTPException, Request

from backend.core.config import settings
from backend.security.caller_identity import CallerAuthError, build_caller_resolver

logger = logging.getLogger("backend")

# ---------------------------------------------------------------------------
# Caller identity & ticket ownership
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,253}$")

_caller_resolver = build_caller_resolver(
    iap_audience=settings.IAP_AUDIENCE,
    frontend_service_account=settings.FRONTEND_SERVICE_ACCOUNT,
    allow_direct_callers=settings.ALLOW_DIRECT_CALLERS,
    dev_email=settings.DEV_FAKE_USER_EMAIL,
)

async def get_caller(request: Request) -> str:
    """Returns the authenticated end-user identity (see backend/security/caller_identity.py).

    Identity comes only from the IAP-signed JWT forwarded by the frontend (verified
    here), or from a direct caller's own Cloud Run-verified ID token. X-User-Email and
    identity fields in request bodies are ignored.
    """
    try:
        caller = await _caller_resolver.resolve(request.headers)
    except CallerAuthError as e:
        logger.warning(f"Rejected request to {request.url.path}: {e}")
        raise HTTPException(status_code=401, detail="Authentication required")
    except Exception as e:
        logger.error(f"Caller identity error on {request.url.path}: {type(e).__name__}")
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _EMAIL_RE.match(caller.email):
        raise HTTPException(status_code=401, detail="Authentication required")
    logger.info(f"Caller authenticated via {caller.source} for {request.url.path}")
    return caller.email

def agent_user_id(caller: str) -> str:
    """Stable, non-reversible per-user key for Agent Runtime sessions (no raw email sent)."""
    return "u_" + hashlib.sha256(caller.encode("utf-8")).hexdigest()[:24]
