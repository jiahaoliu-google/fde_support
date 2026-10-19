"""Backwards-compatible import path; the verifier lives in ``common.iap_auth``
so the backend can verify the same IAP assertion independently."""

from common.iap_auth import (  # noqa: F401
    CLOCK_SKEW_SECONDS,
    EXPECTED_ALGORITHM,
    FORWARDED_IAP_ASSERTION_HEADER,
    IAP_ASSERTION_HEADER,
    IAP_ISSUER,
    IAP_PUBLIC_KEYS_URL,
    IapAuthError,
    IapIdentity,
    IapVerifier,
    build_identity_resolver,
)
