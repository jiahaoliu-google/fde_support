"""
End-user identity for backend requests.

The backend never trusts a plain identity header (``X-User-Email`` is ignored).
The caller's email comes from exactly one of these sources, in order:

1. **IAP (production UI).** The frontend forwards the raw IAP-signed JWT
   (``x-goog-iap-jwt-assertion``) as ``X-Forwarded-Iap-Jwt-Assertion``. The backend
   verifies it independently (ES256, Google's IAP keys, ``aud``, ``iss``, ``exp``) and
   uses its ``email`` claim. Accepted only when the invoker is the frontend service
   account, so a captured assertion can't be replayed by other invokers.

2. **Direct callers (testing, opt-in via ``ALLOW_DIRECT_CALLERS``).** A principal that
   invokes the backend directly with its own Google ID token acts as *itself*: the
   email comes from the token, never from a header, so it cannot claim another
   user's identity or see their tickets.

3. **Local development.** ``DEV_FAKE_USER_EMAIL``, honoured only off Cloud Run.

Anything else fails closed.

About the invoker token: with IAM enforced, Cloud Run verifies the caller's ID token
(signature, audience, expiry, ``run.invoker``) and then replaces the signature with
``SIGNATURE_REMOVED_BY_GOOGLE`` before the request reaches this container. The claims
can therefore only be trusted because the service is deployed with
``--no-allow-unauthenticated`` (enforced by backend/deploy.sh).
TODO(security): if the service is ever made public, paths 1 and 2 lose their
invoker guarantee; path 1 is still protected by the IAP signature, path 2 is not.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Mapping, Optional

from common.iap_auth import (
    CLOCK_SKEW_SECONDS,
    FORWARDED_IAP_ASSERTION_HEADER,
    IapAuthError,
    IapVerifier,
)

logger = logging.getLogger("backend.caller_identity")

CLOUD_RUN_SIGNATURE_REMOVED = "SIGNATURE_REMOVED_BY_GOOGLE"
GOOGLE_ID_TOKEN_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
MAX_INVOKER_TOKEN_LENGTH = 8192


class CallerAuthError(Exception):
    """Request has no acceptable identity. Message is safe to log, not to return."""


@dataclass(frozen=True)
class Caller:
    email: str
    source: str  # "iap" | "direct" | "dev"


def _b64url_json(segment: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        data = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise CallerAuthError(f"malformed invoker token: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise CallerAuthError("malformed invoker token payload")
    return data


def invoker_email(headers: Mapping[str, str], now: Optional[float] = None) -> Optional[str]:
    """Email of the principal Cloud Run authenticated for this request, or None if absent.

    Cloud Run validates ``X-Serverless-Authorization`` instead of ``Authorization`` when
    present, so that header wins.
    """
    authorization = headers.get("x-serverless-authorization") or headers.get("authorization")
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise CallerAuthError("malformed Authorization header")
    if len(token) > MAX_INVOKER_TOKEN_LENGTH:
        raise CallerAuthError("invoker token too large")
    parts = token.split(".")
    if len(parts) != 3:
        raise CallerAuthError("invoker token is not a JWT")
    if parts[2] != CLOUD_RUN_SIGNATURE_REMOVED:
        # Behind Cloud Run IAM the signature is always replaced after verification.
        raise CallerAuthError("invoker token was not verified by Cloud Run")

    claims = _b64url_json(parts[1])
    if claims.get("iss") not in GOOGLE_ID_TOKEN_ISSUERS:
        raise CallerAuthError("invoker token has unexpected issuer")
    exp = claims.get("exp")
    current = time.time() if now is None else now
    if not isinstance(exp, (int, float)) or exp + CLOCK_SKEW_SECONDS < current:
        raise CallerAuthError("invoker token expired")
    email = claims.get("email")
    if not isinstance(email, str) or not email or claims.get("email_verified") is not True:
        raise CallerAuthError("invoker token has no verified email")
    return email.strip().lower()


class CallerResolver:
    def __init__(
        self,
        *,
        iap_verifier: Optional[IapVerifier],
        frontend_service_account: str,
        allow_direct_callers: bool,
        trust_invoker_token: bool,
        dev_email: str = "",
    ) -> None:
        self._iap_verifier = iap_verifier
        self._frontend_sa = frontend_service_account.strip().lower()
        self._allow_direct = allow_direct_callers
        self._trust_invoker = trust_invoker_token
        self._dev_email = dev_email.strip().lower()

    async def resolve(self, headers: Mapping[str, str]) -> Caller:
        # Off Cloud Run nothing has verified the Authorization header, so it is ignored.
        invoker = invoker_email(headers) if self._trust_invoker else None
        assertion = headers.get(FORWARDED_IAP_ASSERTION_HEADER)

        if assertion:
            if self._trust_invoker and (not self._frontend_sa or invoker != self._frontend_sa):
                raise CallerAuthError("IAP assertion is only accepted from the frontend service")
            if self._iap_verifier is None:
                raise CallerAuthError("IAP_AUDIENCE not configured")
            try:
                identity = await self._iap_verifier.verify(assertion)
            except IapAuthError as exc:
                raise CallerAuthError(str(exc)) from exc
            return Caller(email=identity.email, source="iap")

        if invoker:
            if invoker == self._frontend_sa:
                raise CallerAuthError("frontend request without IAP assertion")
            if not self._allow_direct:
                raise CallerAuthError("direct callers are disabled")
            return Caller(email=invoker, source="direct")

        if self._dev_email:
            return Caller(email=self._dev_email, source="dev")
        raise CallerAuthError("no caller identity")


def build_caller_resolver(
    *,
    iap_audience: str,
    frontend_service_account: str,
    allow_direct_callers: bool,
    dev_email: str,
) -> CallerResolver:
    on_cloud_run = bool(os.environ.get("K_SERVICE"))
    iap_audience = iap_audience.strip()
    verifier = IapVerifier(iap_audience) if iap_audience else None

    if on_cloud_run and dev_email:
        logger.error("DEV_FAKE_USER_EMAIL is ignored on Cloud Run.")
        dev_email = ""
    if dev_email:
        logger.warning("Backend identity: DEV_FAKE_USER_EMAIL in use (local development only).")
    if on_cloud_run and verifier is None:
        logger.error("IAP_AUDIENCE is not configured; requests from the UI will be rejected.")
    if allow_direct_callers:
        logger.warning(
            "ALLOW_DIRECT_CALLERS is enabled: principals with run.invoker may call the backend "
            "directly, acting as themselves (testing)."
        )

    return CallerResolver(
        iap_verifier=verifier,
        frontend_service_account=frontend_service_account,
        allow_direct_callers=allow_direct_callers,
        trust_invoker_token=on_cloud_run,
        dev_email=dev_email,
    )
