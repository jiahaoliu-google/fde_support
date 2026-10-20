"""Backend caller identity: IAP JWT verification, invoker checks, and the X-User-Email exploit."""
import asyncio
import base64
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from google.auth import crypt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


import backend.main as main  # noqa: E402
from backend.security.caller_identity import (  # noqa: E402
    CallerAuthError, CallerResolver, build_caller_resolver, invoker_email,
)
from iap_test_utils import (  # noqa: E402
    FRONTEND_SA, TEST_KID, iap_headers, install_backend_test_resolver, make_assertion,
    make_key_pair, make_verifier,
)

ALICE = "alice@example.com"
TESTER = "tester@example.com"
FWD = "x-forwarded-iap-jwt-assertion"


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def cloud_run_token(email: str, signature: str = "SIGNATURE_REMOVED_BY_GOOGLE", **overrides) -> str:
    """ID token as Cloud Run forwards it after verifying it (signature replaced)."""
    claims = {"iss": "https://accounts.google.com", "aud": "https://backend.run.app", "email": email,
              "email_verified": True, "exp": int(time.time()) + 600, "sub": "1"}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return f"{_b64({'alg': 'RS256', 'kid': 'k'})}.{_b64(claims)}.{signature}"


def bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


class TestBackendRejectsHeaderSpoofing(unittest.TestCase):
    """End-to-end through FastAPI (local mode: invoker token not trusted)."""

    def setUp(self):
        install_backend_test_resolver()

    def test_forged_x_user_email_is_rejected(self):
        # The live exploit: invoker access + arbitrary X-User-Email.
        client = TestClient(main.app)
        resp = client.post("/api/tickets/create", json={}, headers={"X-User-Email": ALICE})
        self.assertEqual(resp.status_code, 401)

    def test_assertion_wins_over_x_user_email(self):
        client = TestClient(main.app)
        headers = {**iap_headers(ALICE), "X-User-Email": "ceo@example.com"}
        resp = client.post("/api/tickets/create", json={}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["customer_id"], ALICE)

    def test_invalid_assertion_gets_generic_401(self):
        client = TestClient(main.app)
        resp = client.post("/api/tickets/create", json={}, headers={FWD: "not.a.jwt"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"detail": "Authentication required"})


class TestIapAssertionVerification(unittest.TestCase):
    """Local mode resolver with the test IAP key."""

    def setUp(self):
        self.resolver = CallerResolver(iap_verifier=make_verifier(), frontend_service_account=FRONTEND_SA,
                                       allow_direct_callers=False, trust_invoker_token=False)

    def resolve(self, headers):
        return asyncio.run(self.resolver.resolve(headers))

    def assertRejected(self, headers):
        with self.assertRaises(CallerAuthError):
            self.resolve(headers)

    def test_valid(self):
        caller = self.resolve({FWD: make_assertion("Alice@Example.com")})
        self.assertEqual((caller.email, caller.source), (ALICE, "iap"))

    def test_wrong_audience(self):
        self.assertRejected({FWD: make_assertion(ALICE, aud="/projects/9/global/backendServices/9")})

    def test_expired(self):
        old = int(time.time()) - 7200
        self.assertRejected({FWD: make_assertion(ALICE, iat=old - 600, exp=old)})

    def test_forged_signature(self):
        other, _ = make_key_pair()
        self.assertRejected({FWD: make_assertion(ALICE, signer=crypt.ES256Signer.from_string(other, TEST_KID))})

    def test_unknown_kid(self):
        other, _ = make_key_pair()
        self.assertRejected({FWD: make_assertion(ALICE, signer=crypt.ES256Signer.from_string(other, "nope"))})

    def test_alg_none(self):
        self.assertRejected({FWD: f"{_b64({'alg': 'none', 'kid': TEST_KID})}.{_b64({'email': ALICE})}."})

    def test_hs256(self):
        self.assertRejected({FWD: f"{_b64({'alg': 'HS256', 'kid': TEST_KID})}.{_b64({'email': ALICE})}.c2ln"})

    def test_missing_email(self):
        self.assertRejected({FWD: make_assertion(ALICE, email=None)})

    def test_no_identity(self):
        self.assertRejected({"x-user-email": ALICE})

    def test_authorization_header_ignored_off_cloud_run(self):
        self.assertRejected(bearer(cloud_run_token(TESTER)))


class TestCloudRunInvokerRules(unittest.TestCase):
    """Cloud Run mode: invoker token was verified by the platform."""

    def make(self, allow_direct=True):
        return CallerResolver(iap_verifier=make_verifier(), frontend_service_account=FRONTEND_SA,
                              allow_direct_callers=allow_direct, trust_invoker_token=True)

    def resolve(self, headers, allow_direct=True):
        return asyncio.run(self.make(allow_direct).resolve(headers))

    def assertRejected(self, headers, allow_direct=True):
        with self.assertRaises(CallerAuthError):
            self.resolve(headers, allow_direct)

    def test_frontend_with_assertion(self):
        caller = self.resolve({**bearer(cloud_run_token(FRONTEND_SA)), FWD: make_assertion(ALICE)})
        self.assertEqual((caller.email, caller.source), (ALICE, "iap"))

    def test_assertion_from_other_invoker_rejected(self):
        # A captured assertion can't be replayed by a different principal.
        self.assertRejected({**bearer(cloud_run_token(TESTER)), FWD: make_assertion(ALICE)})

    def test_frontend_without_assertion_rejected(self):
        self.assertRejected(bearer(cloud_run_token(FRONTEND_SA)))

    def test_direct_caller_acts_as_itself(self):
        caller = self.resolve({**bearer(cloud_run_token(TESTER)), "X-User-Email": ALICE})
        self.assertEqual((caller.email, caller.source), (TESTER, "direct"))

    def test_x_serverless_authorization_takes_precedence(self):
        headers = {"x-serverless-authorization": f"Bearer {cloud_run_token(TESTER)}",
                   "authorization": f"Bearer {cloud_run_token(FRONTEND_SA)}"}
        self.assertEqual(self.resolve(headers).email, TESTER)

    def test_direct_callers_disabled(self):
        self.assertRejected(bearer(cloud_run_token(TESTER)), allow_direct=False)

    def test_token_not_verified_by_cloud_run_rejected(self):
        self.assertRejected(bearer(cloud_run_token(TESTER, signature="abc")))

    def test_unverified_email_rejected(self):
        self.assertRejected(bearer(cloud_run_token(TESTER, email_verified=False)))

    def test_expired_invoker_token_rejected(self):
        self.assertRejected(bearer(cloud_run_token(TESTER, exp=int(time.time()) - 3600)))

    def test_wrong_issuer_rejected(self):
        self.assertRejected(bearer(cloud_run_token(TESTER, iss="https://evil.example")))

    def test_malformed_tokens_rejected(self):
        for value in ("Basic abc", "Bearer ", "Bearer a.b", "Bearer a.%%%.SIGNATURE_REMOVED_BY_GOOGLE"):
            with self.assertRaises(CallerAuthError, msg=value):
                invoker_email({"authorization": value})


class TestBuildCallerResolver(unittest.TestCase):
    def test_dev_email_ignored_on_cloud_run(self):
        with patch.dict(os.environ, {"K_SERVICE": "fde-support-backend"}):
            resolver = build_caller_resolver(iap_audience="", frontend_service_account=FRONTEND_SA,
                                             allow_direct_callers=False, dev_email="dev@example.com")
        with self.assertRaises(CallerAuthError):
            asyncio.run(resolver.resolve({}))

    def test_dev_email_used_locally(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("K_SERVICE", None)
            resolver = build_caller_resolver(iap_audience="", frontend_service_account=FRONTEND_SA,
                                             allow_direct_callers=False, dev_email="Dev@Example.com")
        caller = asyncio.run(resolver.resolve({}))
        self.assertEqual((caller.email, caller.source), ("dev@example.com", "dev"))

    def test_no_config_fails_closed(self):
        os.environ.pop("K_SERVICE", None)
        resolver = build_caller_resolver(iap_audience="", frontend_service_account="",
                                         allow_direct_callers=False, dev_email="")
        with self.assertRaises(CallerAuthError):
            asyncio.run(resolver.resolve({FWD: make_assertion(ALICE)}))


if __name__ == "__main__":
    unittest.main()
