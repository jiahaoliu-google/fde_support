#!/usr/bin/env python3
"""Gate 5: Cloud Run zero-traffic canary E2E smoke test.

Probes the deployed `canary---` Cloud Run revision URLs for both `fde-support-backend`
and `fde-support-frontend` BEFORE any production traffic is shifted:
  1. Verifies `GET /health` on `canary---fde-support-frontend` returns 200 OK.
  2. Verifies `GET /api/tickets` on `canary---fde-support-frontend` without an IAP
     assertion returns 401 Unauthorized (confirming the IAP guard is enforced).
  3. Verifies `GET /health` on `canary---fde-support-backend` returns 200 OK.
  4. Calls `POST /api/chat` on `canary---fde-support-backend` with a neutral order
     inquiry containing an email address (`canary-ci@example.com`) and `#10002`:
       - Asserts `routed_to == "FAQ_AGENT"`, `ticket_status == "IN_PROGRESS"`, and `grounded == True`.
       - Fetches `GET /api/tickets/{ticket_id}` and asserts PII redaction (`[EMAIL_REDACTED]`)
         in the stored customer message.
       - Resolves the ticket via `POST /api/tickets/{ticket_id}/resolve`.
  5. Calls `POST /api/chat` on `canary---fde-support-backend` with an urgent message:
       - Asserts `sentiment == "URGENT_FRUSTRATED"`, `routed_to == "HUMAN_ESCALATION"`,
         and `ticket_status == "ESCALATED"`.
       - Resolves the escalated ticket via `POST /api/tickets/{ticket_id}/resolve`.

Exits 0 on success, 1 on any failure (triggering automatic traffic rollback in CI).
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ci.smoke_canary")


def fetch_oidc_id_token(audience: str) -> str:
    """Fetches a Google-signed OIDC ID token for invoking a Cloud Run service."""
    env_token = os.environ.get("CI_CLOUD_RUN_ID_TOKEN", "").strip()
    if env_token:
        return env_token

    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        auth_req = google.auth.transport.requests.Request()
        token = google.oauth2.id_token.fetch_id_token(auth_req, audience)
        if token:
            return token
    except Exception as exc:
        logger.debug("google.oauth2.id_token failed for %s: %s", audience, exc)

    # Workload Identity Federation (GitHub Actions): use ADC access token + IAM generateIdToken
    try:
        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)
        sa_email = (
             getattr(creds, "service_account_email", None)
            or os.environ.get(
                "CI_SERVICE_ACCOUNT_EMAIL",
                "fde-support-ci-eval-sa@fde-support-sandbox-dev-jliu.iam.gserviceaccount.com",
            )
        )
        iam_url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa_email}:generateIdToken"
        resp = httpx.post(
            iam_url,
            headers={"Authorization": f"Bearer {creds.token}"},
            json={"audience": audience, "includeEmail": True},
            timeout=15.0,
        )
        if resp.status_code == 200 and resp.json().get("token"):
            return resp.json()["token"]
    except Exception as exc:
        logger.debug("IAM generateIdToken fallback failed for %s: %s", audience, exc)

    try:
        out = subprocess.check_output(
            ["gcloud", "auth", "print-identity-token"],
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        token = out.decode("utf-8").strip()
        if token:
            return token
    except Exception as exc:
        logger.error("Failed to mint OIDC ID token via gcloud: %s", exc)

    raise RuntimeError(f"Unable to acquire OIDC ID token for audience {audience}")


def run_canary_e2e_smoke(
    backend_url: str,
    frontend_url: str,
    backend_token: str | None = None,
    frontend_token: str | None = None,
) -> bool:
    """Runs live E2E checks against the canary backend and frontend URLs."""
    backend_url = backend_url.rstrip("/")
    frontend_url = frontend_url.rstrip("/")

    try:
        b_token = backend_token or fetch_oidc_id_token(backend_url)
        f_token = frontend_token or fetch_oidc_id_token(frontend_url)
    except Exception as exc:
        logger.error("Authentication setup failed: %s", exc)
        return False

    b_headers = {"Authorization": f"Bearer {b_token}", "Content-Type": "application/json"}
    f_headers = {"Authorization": f"Bearer {f_token}"}

    try:
        with httpx.Client(timeout=90.0) as client:
            # 1. Frontend Canary /health
            logger.info("1/5 Checking Frontend Canary /health at %s ...", frontend_url)
            f_health = client.get(f"{frontend_url}/health", headers=f_headers)
            if f_health.status_code != 200 or f_health.json().get("status") != "healthy":
                logger.error("Frontend canary /health failed: HTTP %s %s", f_health.status_code, f_health.text)
                return False
            logger.info("✅ Frontend Canary /health is healthy.")

            # 2. Frontend Canary IAP Guard (/api/tickets without IAP JWT must return 401)
            logger.info("2/5 Verifying Frontend Canary IAP guard on /api/tickets ...")
            f_unauth = client.get(f"{frontend_url}/api/tickets", headers=f_headers)
            if f_unauth.status_code != 401:
                logger.error(
                    "Frontend canary IAP guard check failed: expected HTTP 401, got %s (%s)",
                    f_unauth.status_code,
                    f_unauth.text,
                )
                return False
            logger.info("✅ Frontend Canary rejected unauthenticated /api/tickets with HTTP 401.")

            # 3. Backend Canary /health
            logger.info("3/5 Checking Backend Canary /health at %s ...", backend_url)
            b_health = client.get(f"{backend_url}/health", headers=b_headers)
            if b_health.status_code != 200 or b_health.json().get("status") != "healthy":
                logger.error("Backend canary /health failed: HTTP %s %s", b_health.status_code, b_health.text)
                return False
            logger.info("✅ Backend Canary /health is healthy (database=%s).", b_health.json().get("database"))

            # 4. Backend Canary Neutral Chat + PII Redaction + Ticket Persistence + Resolve
            logger.info("4/5 Testing Backend Canary Neutral FAQ + PII redaction flow ...")
            neutral_msg = "My email is canary-ci@example.com, what is the status of order #10002?"
            chat_res = client.post(
                f"{backend_url}/api/chat",
                headers=b_headers,
                json={"message": neutral_msg},
            )
            if chat_res.status_code != 200:
                logger.error("Backend canary neutral /api/chat failed: HTTP %s %s", chat_res.status_code, chat_res.text)
                return False

            chat_data = chat_res.json()
            ticket_id = chat_data.get("ticket_id")
            if not ticket_id:
                logger.error("Missing ticket_id in neutral chat response: %s", chat_data)
                return False

            try:
                if (
                    chat_data.get("routed_to") != "Automated RAG Assistant"
                    or chat_data.get("ticket_status") != "IN_PROGRESS"
                    or not chat_data.get("grounded")
                ):
                    logger.error("Unexpected neutral chat response: %s", chat_data)
                    return False

                ticket_res = client.get(f"{backend_url}/api/tickets/{ticket_id}", headers=b_headers)
                if ticket_res.status_code != 200:
                    logger.error("Failed to fetch created ticket %s: HTTP %s", ticket_id, ticket_res.status_code)
                    return False
                messages = ticket_res.json().get("messages") or []
                customer_msgs = [m.get("message_text", "") for m in messages if m.get("sender") == "CUSTOMER"]
                if not customer_msgs or any("canary-ci@example.com" in m for m in customer_msgs):
                    logger.error("PII leak or missing customer message in persisted ticket: %s", customer_msgs)
                    return False
                if not any("[EMAIL_REDACTED]" in m for m in customer_msgs):
                    logger.error("Expected [EMAIL_REDACTED] marker in persisted customer message: %s", customer_msgs)
                    return False
                logger.info("✅ Neutral FAQ chat + PII redaction verified (ticket_id=%s).", ticket_id)
            finally:
                res_close = client.post(f"{backend_url}/api/tickets/{ticket_id}/resolve", headers=b_headers)
                if res_close.status_code != 200:
                    logger.warning("Cleanup resolve for ticket %s returned HTTP %s", ticket_id, res_close.status_code)

            # 5. Backend Canary Urgent/Frustrated Escalation + Resolve
            logger.info("5/5 Testing Backend Canary Urgent Escalation flow ...")
            urgent_msg = "This is unacceptable! You double charged my card and nobody is helping me!"
            urgent_res = client.post(
                f"{backend_url}/api/chat",
                headers=b_headers,
                json={"message": urgent_msg},
            )
            if urgent_res.status_code != 200:
                logger.error("Backend canary urgent /api/chat failed: HTTP %s %s", urgent_res.status_code, urgent_res.text)
                return False

            urgent_data = urgent_res.json()
            urgent_ticket_id = urgent_data.get("ticket_id")
            if not urgent_ticket_id:
                logger.error("Missing ticket_id in urgent escalation response: %s", urgent_data)
                return False

            try:
                if (
                    urgent_data.get("sentiment") != "URGENT_FRUSTRATED"
                    or urgent_data.get("routed_to") != "Human Escalation Queue"
                    or urgent_data.get("ticket_status") != "ESCALATED"
                ):
                    logger.error("Unexpected urgent escalation response: %s", urgent_data)
                    return False
                logger.info("✅ Urgent escalation verified (ticket_id=%s).", urgent_ticket_id)
            finally:
                res_close = client.post(
                    f"{backend_url}/api/tickets/{urgent_ticket_id}/resolve",
                    headers=b_headers,
                )
                if res_close.status_code != 200:
                    logger.warning(
                        "Cleanup resolve for urgent ticket %s returned HTTP %s",
                        urgent_ticket_id,
                        res_close.status_code,
                    )

        logger.info("🎉 All 5 Cloud Run Canary E2E checks passed!")
        return True
    except Exception as exc:
        logger.error("Canary E2E smoke test failed with exception: %s: %s", type(exc).__name__, exc)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run E2E smoke tests against Cloud Run canary URLs.")
    parser.add_argument("--backend-url", required=True, help="Canary backend URL (https://canary---...)")
    parser.add_argument("--frontend-url", required=True, help="Canary frontend URL (https://canary---...)")
    args = parser.parse_args(argv)

    ok = run_canary_e2e_smoke(backend_url=args.backend_url, frontend_url=args.frontend_url)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
