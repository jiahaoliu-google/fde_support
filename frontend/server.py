import os
import certifi
import json
import subprocess
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

app = FastAPI(
    title="FDE Support Portal Web Frontend",
    description="Dedicated Frontend Application serving Customer Support Web Portal with GCP IAP User Identity Extraction."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

INDEX_HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")
if not os.path.exists(INDEX_HTML_PATH):
    INDEX_HTML_PATH = os.path.join(os.getcwd(), "index.html")

CLOUD_RUN_BACKEND_URL = os.environ.get("CLOUD_RUN_BACKEND_URL", "https://fde-support-backend-883790454740.us-central1.run.app").rstrip("/")

def get_iap_user_email(request: Request) -> str:
    """Extracts authenticated user email injected by Google Cloud IAP."""
    raw_header = request.headers.get("x-goog-authenticated-user-email", "").strip()
    if raw_header:
        return raw_header.replace("accounts.google.com:", "").strip()
    return "authenticated_user@demo.com"

import logging

logger = logging.getLogger("frontend")
logging.basicConfig(level=logging.INFO)

def get_id_token(audience: str = CLOUD_RUN_BACKEND_URL) -> str:
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
    user_email = get_iap_user_email(request)
    return {"user_email": user_email}

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

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_to_cloud_run(path: str, request: Request):
    user_email = get_iap_user_email(request)
    target_url = f"{CLOUD_RUN_BACKEND_URL}/api/{path}"
    token = get_id_token(CLOUD_RUN_BACKEND_URL)
    
    # Clean forward_headers to avoid passing IAP/browser headers (like x-goog-iap-jwt-assertion)
    # which cause Cloud Run IAM validation failures on the backend.
    forward_headers = {
        "Accept": request.headers.get("accept", "application/json"),
        "X-User-Email": user_email,
    }
    if request.headers.get("content-type"):
        forward_headers["Content-Type"] = request.headers.get("content-type")
            
    if token:
        forward_headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning(f"No ID token available for backend request to {target_url}")
        
    body = await request.body()
    
    # Inject user_email into JSON body if missing customer_id
    if body and request.method in ["POST", "PUT"]:
        try:
            payload = json.loads(body.decode("utf-8"))
            if isinstance(payload, dict):
                if not payload.get("customer_id") or payload.get("customer_id") in ["Not Set", "cust_default", ""]:
                    payload["customer_id"] = user_email
                body = json.dumps(payload).encode("utf-8")
        except Exception:
            pass

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            proxy_res = await client.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                params=dict(request.query_params),
                content=body
            )
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
        except Exception as e:
            logger.error(f"[PROXY] Error forwarding to {target_url}: {e}")
            return JSONResponse(
                status_code=502,
                content={"error": f"Failed to connect to Cloud Run backend at {target_url}: {str(e)}"}
            )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port)
