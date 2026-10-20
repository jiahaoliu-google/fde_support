import os
import certifi
import logging
import subprocess
import threading
import time
import httpx
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from common.iap_auth import (
    FORWARDED_IAP_ASSERTION_HEADER,
    IAP_ASSERTION_HEADER,
    IapAuthError,
    IapIdentity,
    build_identity_resolver,
)

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

logger = logging.getLogger("frontend")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="FDE Support Portal Web Frontend",
    description="Dedicated Frontend Application serving Customer Support Web Portal with GCP IAP User Identity Extraction."
)

# No CORSMiddleware: the UI (index.html) is served from this same origin and calls
# relative /api/* URLs, so browsers never need cross-origin access. Without CORS headers,
# other sites cannot read responses even if the browser attaches the IAP cookie.

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})  # "none" = user-initiated (address bar)

def is_cross_site_request(request: Request) -> bool:
    """True if a browser indicates the request was initiated by a different site.

    Primary signal is Fetch Metadata (Sec-Fetch-Site), sent by all modern browsers and not
    settable by page scripts. Falls back to comparing Origin with the request host for older
    browsers. Non-browser clients (curl, server-to-server) send neither and are allowed;
    they still need a valid IAP assertion.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site.lower() not in ALLOWED_FETCH_SITES

    origin = request.headers.get("origin")
    if origin is None:
        return False
    if origin == "null":
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlsplit(origin).netloc.lower() != host.split(",")[0].strip().lower()

@app.middleware("http")
async def reject_cross_site_requests(request: Request, call_next):
    """CSRF defense: block state-changing requests initiated by other sites."""
    if request.method.upper() not in SAFE_METHODS and is_cross_site_request(request):
        logger.warning(
            f"Blocked cross-site {request.method} {request.url.path} "
            f"(sec-fetch-site={request.headers.get('sec-fetch-site')}, origin={request.headers.get('origin')})"
        )
        return JSONResponse(status_code=403, content={"detail": "Cross-site request blocked"})
    return await call_next(request)

INDEX_HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")
if not os.path.exists(INDEX_HTML_PATH):
    INDEX_HTML_PATH = os.path.join(os.getcwd(), "index.html")

CLOUD_RUN_BACKEND_URL = os.environ.get("CLOUD_RUN_BACKEND_URL", "https://fde-support-backend-883790454740.us-central1.run.app").rstrip("/")

# Resolves the end-user identity from the cryptographically verified IAP JWT assertion.
# Never trust x-goog-authenticated-user-email or any client-supplied identity field.
_resolve_identity = build_identity_resolver()

async def require_identity(request: Request) -> IapIdentity:
    """Returns the verified IAP identity or raises a generic 401 (fail closed)."""
    try:
        return await _resolve_identity(request.headers)
    except IapAuthError as e:
        logger.warning(f"Rejected request to {request.url.path}: {e}")
        raise HTTPException(status_code=401, detail="Authentication required")
    except Exception as e:
        logger.error(f"IAP verification error on {request.url.path}: {type(e).__name__}")
        raise HTTPException(status_code=401, detail="Authentication required")

ID_TOKEN_REFRESH_MARGIN_SECONDS = 300   # refresh 5 min before expiry
ID_TOKEN_FALLBACK_TTL_SECONDS = 300     # if `exp` can't be read, reuse briefly only
_id_token_cache: dict[str, tuple[str, float]] = {}
_id_token_lock = threading.Lock()

def _token_expiry(token: str) -> float:
    """Reads the `exp` claim WITHOUT verifying the signature.

    Safe here: the token came from Google over TLS and is only used to decide when to
    refresh our own cached credential. The backend (Cloud Run IAM) does the verification.
    """
    try:
        from google.auth import jwt as google_jwt
        exp = google_jwt.decode(token, verify=False).get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
    except Exception:
        pass
    return time.time() + ID_TOKEN_FALLBACK_TTL_SECONDS + ID_TOKEN_REFRESH_MARGIN_SECONDS

def get_id_token(audience: str = CLOUD_RUN_BACKEND_URL) -> str:
    """Returns a cached Google ID token for `audience`, fetching a new one only near expiry."""
    with _id_token_lock:
        cached = _id_token_cache.get(audience)
        if cached and time.time() < cached[1] - ID_TOKEN_REFRESH_MARGIN_SECONDS:
            return cached[0]
        token = _fetch_id_token(audience)
        if token:
            _id_token_cache[audience] = (token, _token_expiry(token))
        else:
            _id_token_cache.pop(audience, None)  # never cache a failure
        return token

def _fetch_id_token(audience: str) -> str:
    # 1. Use official google.oauth2.id_token library
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token
        auth_req = google.auth.transport.requests.Request()
        token = google.oauth2.id_token.fetch_id_token(auth_req, audience)
        if token:
            logger.info(f"Successfully fetched ID token via google.oauth2.id_token for {audience} (len={len(token)})")
            return token
    except Exception as e:
        logger.warning(f"google.oauth2.id_token failed for audience {audience}: {e}")

    # 2. Native GCP Metadata Server HTTP call
    try:
        meta_url = f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience={audience}"
        res = httpx.get(meta_url, headers={"Metadata-Flavor": "Google"}, timeout=5.0)
        logger.info(f"Metadata server response status={res.status_code}, len={len(res.text.strip())}")
        if res.status_code == 200 and res.text.strip():
            return res.text.strip()
    except Exception as e:
        logger.warning(f"Metadata server call failed: {e}")

    # 3. Local execution fallback via gcloud CLI
    try:
        tok = subprocess.check_output(["gcloud", "auth", "print-identity-token"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
        logger.info(f"gcloud fallback token acquired (len={len(tok)})")
        return tok
    except Exception as e:
        logger.warning(f"gcloud CLI fallback failed: {e}")

    return ""

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "FDE Support Web Frontend", "target_backend": CLOUD_RUN_BACKEND_URL}

@app.get("/api/user/me")
async def get_current_user(request: Request):
    identity = await require_identity(request)
    return {"user_email": identity.email}

@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def get_index():
    if os.path.exists(INDEX_HTML_PATH):
        with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
            content = f.read()
            return HTMLResponse(
                content=content,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0"
                }
            )
    return HTMLResponse(content="<h1>FDE Support Portal Frontend</h1>")

@app.exception_handler(404)
async def custom_404_handler(request: Request, exc):
    if not request.url.path.startswith("/api/"):
        return await get_index()
    return JSONResponse(status_code=404, content={"detail": "Not Found"})

async def send_to_backend(method: str, url: str, headers: dict, params: dict, body: bytes) -> httpx.Response:
    """Performs the upstream call to the backend (isolated for testability)."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        return await client.request(method=method, url=url, headers=headers, params=params, content=body)

@app.api_route("/api/{path:path}", methods=["GET", "POST"])
async def proxy_to_cloud_run(path: str, request: Request):
    # Authenticate BEFORE doing any upstream work.
    identity = await require_identity(request)

    target_url = f"{CLOUD_RUN_BACKEND_URL}/api/{path}"
    token = get_id_token(CLOUD_RUN_BACKEND_URL)
    
    # Build upstream headers from scratch: browser/IAP headers (including any client-supplied
    # X-User-Email) are never forwarded. The backend verifies the forwarded IAP assertion
    # itself and takes the user's email from it (backend/security/caller_identity.py).
    forward_headers = {
        "Accept": request.headers.get("accept", "application/json"),
    }
    assertion = request.headers.get(IAP_ASSERTION_HEADER)
    if assertion:
        forward_headers[FORWARDED_IAP_ASSERTION_HEADER] = assertion  # already verified above; never logged
    if request.headers.get("content-type"):
        forward_headers["Content-Type"] = request.headers.get("content-type")
            
    if token:
        forward_headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning(f"No ID token available for backend request to {target_url}")
        
    # Body is forwarded unchanged; the backend ignores any identity fields in it.
    body = await request.body()

    try:
        proxy_res = await send_to_backend(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            params=dict(request.query_params),
            body=body,
        )
    except Exception as e:
        logger.error(f"[PROXY] Error forwarding to {target_url}: {type(e).__name__}: {e}")
        return JSONResponse(status_code=502, content={"error": "Backend service is unavailable. Please try again later."})

    logger.info(f"[PROXY] {request.method} {target_url} -> status {proxy_res.status_code}")
    
    response_headers = {}
    for k, v in proxy_res.headers.items():
        if k.lower() not in ["content-length", "content-encoding", "transfer-encoding"]:
            response_headers[k] = v
            
    return Response(
        content=proxy_res.content,
        status_code=proxy_res.status_code,
        headers=response_headers
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port)
