"""
Verification of Google Cloud IAP signed headers (``x-goog-iap-jwt-assertion``).

IAP signs every request it forwards with an ES256 JWT. Unlike the plain
``x-goog-authenticated-user-email`` header, this assertion cannot be forged by a
caller that bypasses IAP (e.g. by hitting the ``*.run.app`` URL directly), so it
is the only trustworthy source of the end-user identity.

Checks performed (fail closed on any error):
  * algorithm is hard-coded to ES256 (``none`` / RS256 / HS256 are rejected)
  * signature against Google's published IAP public keys (cached, refreshed on
    unknown ``kid`` or after TTL)
  * ``aud`` equals this deployment's IAP audience (``IAP_AUDIENCE`` env var)
  * ``iss`` equals ``https://cloud.google.com/iap``
  * ``iat`` / ``exp`` within a small clock-skew window
  * ``email`` and ``sub`` claims present

Used by the frontend (on the request from IAP) and, independently, by the backend
(on the assertion the frontend forwards as ``X-Forwarded-Iap-Jwt-Assertion``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Optional

import httpx
from google.auth import jwt as google_jwt

logger = logging.getLogger("common.iap_auth")

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_PUBLIC_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ASSERTION_HEADER = "x-goog-iap-jwt-assertion"
# Frontend -> backend. Not an ``X-Goog-*`` name, which Google's front ends may treat specially.
FORWARDED_IAP_ASSERTION_HEADER = "x-forwarded-iap-jwt-assertion"
EXPECTED_ALGORITHM = "ES256"
CERT_CACHE_TTL_SECONDS = 3600
CLOCK_SKEW_SECONDS = 30
MAX_ASSERTION_LENGTH = 8192

CertsFetcher = Callable[[], Awaitable[Mapping[str, str]]]


class IapAuthError(Exception):
    """Raised when an IAP assertion is missing or fails verification.

    The message is safe to log but must not be returned verbatim to clients.
    """


@dataclass(frozen=True)
class IapIdentity:
    email: str
    sub: str


async def _fetch_iap_public_keys() -> Mapping[str, str]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(IAP_PUBLIC_KEYS_URL)
        resp.raise_for_status()
        keys = resp.json()
    if not isinstance(keys, dict) or not keys:
        raise IapAuthError("IAP public key set is empty or malformed")
    return keys


class IapVerifier:
    """Verifies IAP JWT assertions for a single expected audience."""

    def __init__(
        self,
        audience: str,
        certs_fetcher: CertsFetcher = _fetch_iap_public_keys,
        cert_ttl_seconds: int = CERT_CACHE_TTL_SECONDS,
    ) -> None:
        if not audience:
            raise ValueError("IAP audience must be a non-empty string")
        self._audience = audience
        self._certs_fetcher = certs_fetcher
        self._cert_ttl = cert_ttl_seconds
        self._certs: Mapping[str, str] = {}
        self._certs_fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def _get_certs(self, force_refresh: bool = False) -> Mapping[str, str]:
        fresh = self._certs and (time.monotonic() - self._certs_fetched_at) < self._cert_ttl
        if fresh and not force_refresh:
            return self._certs
        async with self._lock:
            # Re-check after acquiring the lock (another task may have refreshed).
            fresh = self._certs and (time.monotonic() - self._certs_fetched_at) < self._cert_ttl
            if fresh and not force_refresh:
                return self._certs
            self._certs = await self._certs_fetcher()
            self._certs_fetched_at = time.monotonic()
            return self._certs

    async def verify(self, assertion: Optional[str]) -> IapIdentity:
        if not assertion:
            raise IapAuthError("missing IAP assertion")
        if len(assertion) > MAX_ASSERTION_LENGTH:
            raise IapAuthError("IAP assertion too large")

        try:
            header = google_jwt.decode_header(assertion)
        except Exception as exc:
            raise IapAuthError(f"malformed IAP assertion: {type(exc).__name__}") from exc

        # Hard-code the algorithm; never trust the token's own claim about how to verify it.
        if header.get("alg") != EXPECTED_ALGORITHM:
            raise IapAuthError(f"unexpected JWT algorithm: {header.get('alg')!r}")
        key_id = header.get("kid")
        if not key_id:
            raise IapAuthError("IAP assertion has no key id")

        certs = await self._get_certs()
        if key_id not in certs:
            # Keys rotate; refresh once before rejecting.
            certs = await self._get_certs(force_refresh=True)
            if key_id not in certs:
                raise IapAuthError("IAP assertion signed with unknown key id")

        try:
            claims = google_jwt.decode(
                assertion,
                certs={key_id: certs[key_id]},
                audience=self._audience,
                clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
            )
        except Exception as exc:
            raise IapAuthError(f"IAP assertion verification failed: {type(exc).__name__}") from exc

        if claims.get("iss") != IAP_ISSUER:
            raise IapAuthError("IAP assertion has unexpected issuer")

        email = claims.get("email")
        sub = claims.get("sub")
        if not isinstance(email, str) or not email or not isinstance(sub, str) or not sub:
            raise IapAuthError("IAP assertion missing email/sub claims")

        return IapIdentity(email=email.strip().lower(), sub=sub)


def _running_on_cloud_run() -> bool:
    return bool(os.environ.get("K_SERVICE"))


def build_identity_resolver() -> Callable[[Mapping[str, str]], Awaitable[IapIdentity]]:
    """Builds the request-identity resolver from environment configuration.

    * ``IAP_AUDIENCE`` set      -> verify IAP assertions (production path).
    * ``IAP_AUDIENCE`` unset    -> fail closed (every request rejected), unless
      ``DEV_FAKE_USER_EMAIL`` is set AND the process is NOT running on Cloud Run,
      in which case a fixed local-development identity is used.
    """
    audience = os.environ.get("IAP_AUDIENCE", "").strip()
    if audience:
        verifier = IapVerifier(audience)

        async def resolve(headers: Mapping[str, str]) -> IapIdentity:
            return await verifier.verify(headers.get(IAP_ASSERTION_HEADER))

        return resolve

    dev_email = os.environ.get("DEV_FAKE_USER_EMAIL", "").strip().lower()
    if dev_email and not _running_on_cloud_run():
        logger.warning(
            "IAP verification DISABLED: using DEV_FAKE_USER_EMAIL=%s for local development only.",
            dev_email,
        )
        dev_identity = IapIdentity(email=dev_email, sub=f"dev:{dev_email}")

        async def resolve_dev(headers: Mapping[str, str]) -> IapIdentity:
            return dev_identity

        return resolve_dev

    logger.error("IAP_AUDIENCE is not configured; all API requests will be rejected (fail closed).")

    async def reject(headers: Mapping[str, str]) -> IapIdentity:
        raise IapAuthError("IAP_AUDIENCE not configured")

    return reject
