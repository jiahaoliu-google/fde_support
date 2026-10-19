"""Frontend identity resolver configuration (real common.iap_auth; signature checks are in test_authz)."""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from common.iap_auth import IapAuthError, build_identity_resolver  # noqa: E402


def _env(**values):
    env = {k: v for k, v in os.environ.items() if k not in ("IAP_AUDIENCE", "DEV_FAKE_USER_EMAIL", "K_SERVICE")}
    env.update(values)
    return patch.dict(os.environ, env, clear=True)


class TestIdentityResolverConfig(unittest.TestCase):
    def test_unsigned_email_header_is_not_trusted(self):
        with _env(IAP_AUDIENCE="/projects/1/global/backendServices/2"):
            resolve = build_identity_resolver()
        with self.assertRaises(IapAuthError):
            asyncio.run(resolve({"x-goog-authenticated-user-email": "accounts.google.com:ceo@x.com"}))

    def test_no_audience_fails_closed(self):
        with _env():
            resolve = build_identity_resolver()
        with self.assertRaises(IapAuthError):
            asyncio.run(resolve({"x-goog-iap-jwt-assertion": "anything"}))

    def test_dev_identity_only_off_cloud_run(self):
        with _env(DEV_FAKE_USER_EMAIL="Dev@Example.com"):
            resolve = build_identity_resolver()
        self.assertEqual(asyncio.run(resolve({})).email, "dev@example.com")

        with _env(DEV_FAKE_USER_EMAIL="dev@example.com", K_SERVICE="fde-support-frontend"):
            resolve = build_identity_resolver()
        with self.assertRaises(IapAuthError):
            asyncio.run(resolve({}))


if __name__ == "__main__":
    unittest.main()
