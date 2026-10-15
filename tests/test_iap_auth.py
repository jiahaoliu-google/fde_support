import unittest

def mock_verify_iap_header(headers: dict) -> dict:
    user_email_header = headers.get("X-Goog-Authenticated-User-Email", "")
    iap_jwt = headers.get("X-Goog-IAP-JWT-Assertion", "")
    user_email = user_email_header.replace("accounts.google.com:", "") if user_email_header else "anonymous_user@cloudidentity.internal"
    return {
        "authenticated": True if user_email_header or iap_jwt else False,
        "email": user_email,
        "jwt_present": bool(iap_jwt)
    }

class TestIAPAuth(unittest.TestCase):
    def test_iap_header_verification(self):
        headers = {
            "X-Goog-Authenticated-User-Email": "accounts.google.com:testuser@example.com",
            "X-Goog-IAP-JWT-Assertion": "dummy_jwt_token"
        }
        res = mock_verify_iap_header(headers)
        self.assertTrue(res["authenticated"])
        self.assertEqual(res["email"], "testuser@example.com")
        self.assertTrue(res["jwt_present"])

    def test_iap_header_fallback(self):
        headers = {}
        res = mock_verify_iap_header(headers)
        self.assertFalse(res["authenticated"])
        self.assertIn("anonymous_user", res["email"])

if __name__ == '__main__':
    unittest.main()
