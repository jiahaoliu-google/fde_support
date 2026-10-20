"""Security regression tests: identity spoofing & IDOR on tickets, and IAP JWT verification."""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from google.auth import crypt
from google.auth import jwt as google_jwt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


from backend.main import app as backend_app  # noqa: E402

from backend.clients.agent_runtime import rag_circuit_breaker  # noqa: E402
from iap_test_utils import iap_headers  # noqa: E402
from common.iap_auth import IapAuthError, IapVerifier, IAP_ISSUER  # noqa: E402

ALICE = "alice@example.com"
BOB = "bob@example.com"
NEUTRAL = {"sentiment": "NEUTRAL_FACTUAL", "extracted_entities": {}}
FAQ_OK = {"answer": "Returns are accepted within 30 days.", "citations": [], "grounded": True}


# ---------------------------------------------------------------------------
# Backend: ownership / IDOR
# ---------------------------------------------------------------------------
class TestTicketOwnership(unittest.TestCase):
    def setUp(self):
        rag_circuit_breaker.record_success()
        self.alice = TestClient(backend_app, headers=iap_headers(ALICE))
        self.bob = TestClient(backend_app, headers=iap_headers(BOB))
        self.anon = TestClient(backend_app)
        self.ticket_id = self.alice.post("/api/tickets/create", json={}).json()["ticket_id"]

    def test_requests_without_identity_are_rejected(self):
        self.assertEqual(self.anon.post("/api/tickets/create", json={}).status_code, 401)
        self.assertEqual(self.anon.get(f"/api/tickets/{self.ticket_id}").status_code, 401)
        self.assertEqual(self.anon.post(f"/api/tickets/{self.ticket_id}/resolve").status_code, 401)
        self.assertEqual(self.anon.post("/api/chat", json={"message": "hi"}).status_code, 401)

    def test_malformed_identity_header_rejected(self):
        bad = TestClient(backend_app, headers={"X-User-Email": "not-an-email"})
        self.assertEqual(bad.post("/api/tickets/create", json={}).status_code, 401)

    def test_ticket_id_is_server_generated_and_client_values_ignored(self):
        resp = self.alice.post(
            "/api/tickets/create",
            json={"session_id": "attacker_chosen_id", "customer_id": "ceo@example.com"},
        ).json()
        self.assertNotEqual(resp["ticket_id"], "attacker_chosen_id")
        self.assertEqual(len(resp["ticket_id"]), 36)  # uuid4
        self.assertEqual(resp["customer_id"], ALICE)

    def test_owner_can_read_own_ticket(self):
        resp = self.alice.get(f"/api/tickets/{self.ticket_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["ticket"]["customer_id"], ALICE)

    def test_other_user_cannot_read_ticket(self):
        self.assertEqual(self.bob.get(f"/api/tickets/{self.ticket_id}").status_code, 404)

    def test_other_user_cannot_resolve_ticket(self):
        self.assertEqual(self.bob.post(f"/api/tickets/{self.ticket_id}/resolve").status_code, 404)
        self.assertNotEqual(
            self.alice.get(f"/api/tickets/{self.ticket_id}").json()["ticket"]["current_status"],
            "RESOLVED",
        )

    def test_resolve_does_not_create_unknown_tickets(self):
        self.assertEqual(self.alice.post("/api/tickets/does-not-exist/resolve").status_code, 404)
        self.assertEqual(self.alice.get("/api/tickets/does-not-exist").status_code, 404)

    @patch("backend.clients.agent_runtime.call_faq_agent")
    @patch("backend.clients.agent_runtime.call_sentiment_agent")
    def test_other_user_cannot_chat_on_ticket(self, mock_sentiment, mock_faq):
        mock_sentiment.return_value = NEUTRAL
        mock_faq.return_value = FAQ_OK
        resp = self.bob.post("/api/chat", json={"message": "What did we discuss?", "session_id": self.ticket_id})
        self.assertEqual(resp.status_code, 404)
        # Rejected before any LLM call -> no leakage through agent memory.
        mock_sentiment.assert_not_called()
        mock_faq.assert_not_called()

    @patch("backend.clients.agent_runtime.call_faq_agent")
    @patch("backend.clients.agent_runtime.call_sentiment_agent")
    def test_body_customer_id_is_ignored(self, mock_sentiment, mock_faq):
        mock_sentiment.return_value = NEUTRAL
        mock_faq.return_value = FAQ_OK
        resp = self.bob.post("/api/chat", json={"message": "Return policy?", "customer_id": ALICE})
        self.assertEqual(resp.status_code, 200)
        new_id = resp.json()["ticket_id"]
        self.assertEqual(self.bob.get(f"/api/tickets/{new_id}").json()["ticket"]["customer_id"], BOB)
        self.assertEqual(self.alice.get(f"/api/tickets/{new_id}").status_code, 404)

    @patch("backend.clients.agent_runtime.call_faq_agent")
    @patch("backend.clients.agent_runtime.call_sentiment_agent")
    def test_agent_sessions_are_scoped_per_user(self, mock_sentiment, mock_faq):
        mock_sentiment.return_value = NEUTRAL
        mock_faq.return_value = FAQ_OK
        self.alice.post("/api/chat", json={"message": "Return policy?", "session_id": self.ticket_id})
        self.bob.post("/api/chat", json={"message": "Return policy?"})
        user_ids = [c.args[2] for c in mock_faq.call_args_list]
        self.assertEqual(len(set(user_ids)), 2)
        for uid in user_ids:
            self.assertNotIn("@", uid)  # raw email is not sent to the agent runtime


# ---------------------------------------------------------------------------
# Frontend: IAP JWT verification
# ---------------------------------------------------------------------------
AUDIENCE = "/projects/123/global/backendServices/456"


def _make_key():
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return priv_pem, pub_pem


class TestIapVerifier(unittest.TestCase):
    KID = "test-kid"

    def setUp(self):
        self.priv_pem, self.pub_pem = _make_key()
        self.signer = crypt.ES256Signer.from_string(self.priv_pem, self.KID)
        self.fetch_count = 0

        async def fetch():
            self.fetch_count += 1
            return {self.KID: self.pub_pem}

        self.verifier = IapVerifier(AUDIENCE, certs_fetcher=fetch)

    def _token(self, signer=None, **overrides):
        now = int(time.time())
        claims = {"iss": IAP_ISSUER, "aud": AUDIENCE, "sub": "accounts.google.com:1", "email": "Alice@Example.com",
                  "iat": now, "exp": now + 600}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return google_jwt.encode(signer or self.signer, claims).decode()

    def verify(self, token):
        return asyncio.run(self.verifier.verify(token))

    def test_valid_token(self):
        ident = self.verify(self._token())
        self.assertEqual(ident.email, "alice@example.com")
        self.assertEqual(ident.sub, "accounts.google.com:1")

    def test_missing_token(self):
        with self.assertRaises(IapAuthError):
            self.verify(None)

    def test_wrong_audience(self):
        with self.assertRaises(IapAuthError):
            self.verify(self._token(aud="/projects/999/global/backendServices/1"))

    def test_wrong_issuer(self):
        with self.assertRaises(IapAuthError):
            self.verify(self._token(iss="https://accounts.google.com"))

    def test_expired(self):
        old = int(time.time()) - 3600
        with self.assertRaises(IapAuthError):
            self.verify(self._token(iat=old - 600, exp=old))

    def test_forged_signature(self):
        other_priv, _ = _make_key()
        forger = crypt.ES256Signer.from_string(other_priv, self.KID)
        with self.assertRaises(IapAuthError):
            self.verify(self._token(signer=forger))

    def test_unknown_kid_refreshes_once_then_rejects(self):
        other_priv, _ = _make_key()
        unknown = crypt.ES256Signer.from_string(other_priv, "unknown-kid")
        with self.assertRaises(IapAuthError):
            self.verify(self._token(signer=unknown))
        self.assertEqual(self.fetch_count, 2)

    def test_alg_none_rejected(self):
        import base64, json
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        token = f"{enc({'alg': 'none', 'kid': self.KID})}.{enc({'email': 'evil@x.com', 'aud': AUDIENCE})}."
        with self.assertRaises(IapAuthError):
            self.verify(token)

    def test_missing_email(self):
        with self.assertRaises(IapAuthError):
            self.verify(self._token(email=None))


# ---------------------------------------------------------------------------
# Frontend proxy: identity forwarding
# ---------------------------------------------------------------------------
class _FrontendProxyFixture(unittest.TestCase):
    """Frontend app with a fake IAP resolver and a captured (not sent) upstream call."""

    def setUp(self):
        import frontend.server as server
        from common.iap_auth import IapIdentity

        self.server = server
        self.captured = {}

        async def fake_resolver(headers):
            if headers.get("x-goog-iap-jwt-assertion") != "valid":
                raise IapAuthError("missing IAP assertion")
            return IapIdentity(email=ALICE, sub="accounts.google.com:1")

        async def fake_send(method, url, headers, params, body):
            import httpx
            self.captured = {"headers": headers, "body": body}
            return httpx.Response(200, json={"ok": True})

        self.patches = [
            patch.object(server, "_resolve_identity", fake_resolver),
            patch.object(server, "send_to_backend", fake_send),
            patch.object(server, "get_id_token", lambda *a, **k: "tok"),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(server.app)

    def tearDown(self):
        for p in self.patches:
            p.stop()


class TestFrontendProxyIdentity(_FrontendProxyFixture):
    def test_spoofed_email_header_without_assertion_rejected(self):
        resp = self.client.get("/api/user/me", headers={"x-goog-authenticated-user-email": "accounts.google.com:ceo@x.com"})
        self.assertEqual(resp.status_code, 401)
        resp = self.client.post("/api/chat", json={"message": "hi"}, headers={"X-User-Email": "ceo@x.com"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.captured, {})  # nothing forwarded upstream

    def test_verified_identity_overrides_client_headers(self):
        resp = self.client.post(
            "/api/chat",
            json={"message": "hi", "customer_id": "ceo@x.com"},
            headers={"x-goog-iap-jwt-assertion": "valid", "X-User-Email": "ceo@x.com",
                     "x-goog-authenticated-user-email": "accounts.google.com:ceo@x.com"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("X-User-Email", self.captured["headers"])
        self.assertNotIn("X-User-Sub", self.captured["headers"])
        # The raw IAP assertion is forwarded so the backend can verify identity itself.
        self.assertEqual(self.captured["headers"]["x-forwarded-iap-jwt-assertion"], "valid")

    def test_client_cannot_inject_forwarded_assertion(self):
        resp = self.client.post(
            "/api/chat", json={"message": "hi"},
            headers={"x-goog-iap-jwt-assertion": "valid", "X-Forwarded-Iap-Jwt-Assertion": "attacker"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.captured["headers"]["x-forwarded-iap-jwt-assertion"], "valid")

    def test_user_me_returns_verified_email(self):
        resp = self.client.get("/api/user/me", headers={"x-goog-iap-jwt-assertion": "valid"})
        self.assertEqual(resp.json(), {"user_email": ALICE})

    def test_put_delete_not_allowed(self):
        self.assertEqual(self.client.delete("/api/tickets/x", headers={"x-goog-iap-jwt-assertion": "valid"}).status_code, 405)


# ---------------------------------------------------------------------------
# CORS / CSRF
# ---------------------------------------------------------------------------
EVIL = "https://evil.example"
AUTH = {"x-goog-iap-jwt-assertion": "valid"}


class TestCorsAndCsrf(_FrontendProxyFixture):
    """Reuses the proxy fixture (fake IAP resolver + captured upstream call)."""

    def assertNoCorsHeaders(self, resp):
        self.assertNotIn("access-control-allow-origin", resp.headers)
        self.assertNotIn("access-control-allow-credentials", resp.headers)

    def test_foreign_origin_gets_no_cors_headers(self):
        resp = self.client.get("/api/user/me", headers={**AUTH, "Origin": EVIL})
        self.assertEqual(resp.status_code, 200)
        self.assertNoCorsHeaders(resp)

    def test_preflight_from_foreign_origin_not_granted(self):
        resp = self.client.options(
            "/api/chat",
            headers={"Origin": EVIL, "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "content-type"},
        )
        self.assertNoCorsHeaders(resp)

    def test_cross_site_post_blocked_before_proxying(self):
        for path in ("/api/chat", "/api/tickets/create", "/api/tickets/abc/resolve"):
            resp = self.client.post(path, json={"message": "hi"}, headers={**AUTH, "Sec-Fetch-Site": "cross-site"})
            self.assertEqual(resp.status_code, 403, path)
        self.assertEqual(self.captured, {})

    def test_same_site_subdomain_post_blocked(self):
        resp = self.client.post("/api/chat", json={"message": "hi"}, headers={**AUTH, "Sec-Fetch-Site": "same-site"})
        self.assertEqual(resp.status_code, 403)

    def test_same_origin_post_allowed(self):
        resp = self.client.post("/api/chat", json={"message": "hi"}, headers={**AUTH, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(resp.status_code, 200)

    def test_origin_fallback_without_fetch_metadata(self):
        blocked = self.client.post("/api/chat", json={"message": "hi"}, headers={**AUTH, "Origin": EVIL})
        self.assertEqual(blocked.status_code, 403)
        null_origin = self.client.post("/api/chat", json={"message": "hi"}, headers={**AUTH, "Origin": "null"})
        self.assertEqual(null_origin.status_code, 403)
        same = self.client.post("/api/chat", json={"message": "hi"}, headers={**AUTH, "Origin": "http://testserver"})
        self.assertEqual(same.status_code, 200)

    def test_non_browser_client_allowed(self):
        resp = self.client.post("/api/chat", json={"message": "hi"}, headers=AUTH)
        self.assertEqual(resp.status_code, 200)

    def test_cross_site_get_not_blocked_but_unreadable(self):
        # Top-level navigation/links stay functional; lack of CORS headers keeps the body unreadable.
        resp = self.client.get("/api/user/me", headers={**AUTH, "Sec-Fetch-Site": "cross-site", "Origin": EVIL})
        self.assertEqual(resp.status_code, 200)
        self.assertNoCorsHeaders(resp)


class TestBackendNoCors(unittest.TestCase):
    def test_backend_emits_no_cors_headers(self):
        client = TestClient(backend_app, headers=iap_headers(ALICE))
        resp = client.get("/health", headers={"Origin": EVIL})
        self.assertNotIn("access-control-allow-origin", resp.headers)
        resp = client.options("/api/chat", headers={"Origin": EVIL, "Access-Control-Request-Method": "POST"})
        self.assertNotIn("access-control-allow-origin", resp.headers)


if __name__ == "__main__":
    unittest.main()
