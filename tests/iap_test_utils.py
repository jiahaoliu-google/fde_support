"""Test helpers: real ES256-signed IAP assertions and a backend resolver that trusts the test key.

Exercises the production verification code (common.iap_auth / backend.security.caller_identity);
only the public-key source is replaced.
"""
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from google.auth import crypt
from google.auth import jwt as google_jwt

from common.iap_auth import FORWARDED_IAP_ASSERTION_HEADER, IAP_ISSUER, IapVerifier

import no_network  # noqa: F401  (blocks outbound network for every test using these helpers)

TEST_AUDIENCE = "/projects/123/global/backendServices/456"
TEST_KID = "test-kid"
FRONTEND_SA = "fde-support-frontend-sa@test-project.iam.gserviceaccount.com"


def make_key_pair():
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return priv_pem, pub_pem


_PRIV_PEM, _PUB_PEM = make_key_pair()
SIGNER = crypt.ES256Signer.from_string(_PRIV_PEM, TEST_KID)


async def _test_certs():
    return {TEST_KID: _PUB_PEM}


def make_verifier(audience: str = TEST_AUDIENCE) -> IapVerifier:
    return IapVerifier(audience, certs_fetcher=_test_certs)


def make_assertion(user_email: str, signer=None, **overrides) -> str:
    now = int(time.time())
    claims = {"iss": IAP_ISSUER, "aud": TEST_AUDIENCE, "sub": f"accounts.google.com:{user_email}",
              "email": user_email, "iat": now, "exp": now + 3600}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return google_jwt.encode(signer or SIGNER, claims).decode()


def iap_headers(email: str) -> dict:
    """Headers the frontend sends to the backend for a verified IAP user."""
    install_backend_test_resolver()
    return {FORWARDED_IAP_ASSERTION_HEADER: make_assertion(email)}


def install_backend_test_resolver():
    """Point backend.api.deps at a resolver that trusts the test signing key (idempotent)."""
    from backend.api import deps
    from backend.security.caller_identity import CallerResolver

    if getattr(deps._caller_resolver, "_is_test_resolver", False):
        return
    resolver = CallerResolver(
        iap_verifier=make_verifier(),
        frontend_service_account=FRONTEND_SA,
        allow_direct_callers=False,
        trust_invoker_token=False,  # local tests: nothing verified the Authorization header
    )
    resolver._is_test_resolver = True
    deps._caller_resolver = resolver
