#!/usr/bin/env python3
"""
Method 2 Auth Proxy:
Listens locally at http://127.0.0.1:3000/
Automatically fetches a fresh gcloud identity token for every request,
injects 'Authorization: Bearer <TOKEN>', and proxies requests to:
https://fde-support-frontend-883790454740.us-central1.run.app
"""

import os
import subprocess
import logging
import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("auth_proxy")

TARGET_FRONTEND_URL = os.environ.get(
    "TARGET_FRONTEND_URL",
    "https://fde-support-frontend-883790454740.us-central1.run.app"
).rstrip("/")

app = FastAPI(
    title="Local Cloud Run Auth Injector Proxy",
    description="Injects gcloud identity tokens for local web browser access to authenticated Cloud Run services."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_identity_token() -> str:
    """Retrieves fresh Google identity token via gcloud CLI."""
    try:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-identity-token"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
        if token:
            return token
    except Exception as e:
        logger.error(f"Failed to fetch gcloud identity token: {e}")
    return ""

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_handler(path: str, request: Request):
    target_url = f"{TARGET_FRONTEND_URL}/{path}" if path else TARGET_FRONTEND_URL
    if request.query_params:
        target_url = f"{target_url}?{request.query_params}"

    token = get_identity_token()
    if not token:
        return JSONResponse(
            status_code=401,
            content={"error": "Unable to acquire gcloud identity token. Please run 'gcloud auth login' in terminal."}
        )

    # Build forward headers
    headers = {}
    for k, v in request.headers.items():
        if k.lower() not in ["host", "content-length", "transfer-encoding", "connection", "authorization"]:
            headers[k] = v

    headers["Authorization"] = f"Bearer {token}"
    body = await request.body()

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        try:
            proxy_res = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body
            )

            res_headers = {}
            for k, v in proxy_res.headers.items():
                if k.lower() not in ["content-length", "content-encoding", "transfer-encoding"]:
                    res_headers[k] = v

            return Response(
                content=proxy_res.content,
                status_code=proxy_res.status_code,
                headers=res_headers
            )
        except Exception as e:
            logger.error(f"Error forwarding request to {target_url}: {e}")
            return JSONResponse(
                status_code=502,
                content={"error": f"Failed to connect to Cloud Run Frontend at {target_url}: {str(e)}"}
            )

if __name__ == "__main__":
    print("=" * 65)
    print("  Local Cloud Run Auth Injector Proxy Started")
    print("  Local URL: http://127.0.0.1:3000/")
    print(f"  Target Cloud Run Frontend: {TARGET_FRONTEND_URL}")
    print("  Open http://127.0.0.1:3000 in your browser to access the portal!")
    print("=" * 65)
    uvicorn.run(app, host="127.0.0.1", port=3000, log_level="warning")
