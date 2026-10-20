"""Cloud DLP layer of the Model Armor redactor, with a fake client (no network)."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.security import model_armor as MA  # noqa: E402
from backend.security.model_armor import ModelArmorRedactor, PATH_DLP, PATH_REGEX  # noqa: E402


class _Resp:
    def __init__(self, value):
        self.item = type("Item", (), {"value": value})()


class FakeDlp:
    """Mimics DlpServiceClient.deidentify_content using the configured tokens."""

    def __init__(self, fail=None, value=None):
        self.calls, self.fail, self.value = [], fail, value

    def deidentify_content(self, request, timeout=None):
        self.calls.append((request, timeout))
        if self.fail:
            raise self.fail
        if self.value is not None:
            return _Resp(self.value)
        text = request["item"]["value"]
        # Pretend DLP found the email only; the regex layer must still catch the SSN.
        return _Resp(text.replace("jane@example.com", "[EMAIL_REDACTED]"))


MSG = "card 4111 1111 1111 1111, ssn 123-45-6789, email jane@example.com, order #10001"


class TestDlpLayer(unittest.TestCase):
    def test_request_shape_and_tokens(self):
        fake = FakeDlp()
        r = ModelArmorRedactor(project_id="p1", dlp_enabled=True, timeout_s=2.5, client=fake)
        out, path = r.redact_with_path(MSG)
        self.assertEqual(path, PATH_DLP)
        req, timeout = fake.calls[0]
        self.assertEqual(timeout, 2.5)
        self.assertEqual(req["parent"], "projects/p1/locations/global")
        self.assertEqual({i["name"] for i in req["inspect_config"]["info_types"]},
                         set(MA.INFO_TYPE_TOKENS))
        tokens = {t["info_types"][0]["name"]:
                  t["primitive_transformation"]["replace_config"]["new_value"]["string_value"]
                  for t in req["deidentify_config"]["info_type_transformations"]["transformations"]}
        self.assertEqual(tokens, MA.INFO_TYPE_TOKENS)
        self.assertEqual(req["inspect_config"]["min_likelihood_per_info_type"][0]["min_likelihood"], "LIKELY")
        # DLP output used, regex layer still applied, order ID kept.
        self.assertEqual(out, "card [CREDIT_CARD_REDACTED], ssn [SSN_REDACTED], "
                              "email [EMAIL_REDACTED], order #10001")

    def test_dlp_error_falls_back_to_regex_and_cools_down(self):
        fake = FakeDlp(fail=RuntimeError("PERMISSION_DENIED"))
        r = ModelArmorRedactor(project_id="p1", dlp_enabled=True, client=fake)
        with self.assertLogs(MA.logger, "ERROR") as logs:
            out, path = r.redact_with_path(MSG)
        self.assertEqual(path, PATH_REGEX)
        self.assertNotIn("jane@example.com", out)
        self.assertNotIn("123-45-6789", out)
        self.assertNotIn(MSG, "\n".join(logs.output))          # customer text never logged
        r.redact_with_path(MSG)                                  # within cooldown: no 2nd call
        self.assertEqual(len(fake.calls), 1)
        r._skip_dlp_until = time.monotonic() - 1                 # cooldown over: retries
        r.redact_with_path(MSG)
        self.assertEqual(len(fake.calls), 2)

    def test_empty_dlp_value_is_ignored(self):
        r = ModelArmorRedactor(dlp_enabled=True, client=FakeDlp(value=""))
        out, path = r.redact_with_path(MSG)
        self.assertEqual(path, PATH_REGEX)
        self.assertNotIn("jane@example.com", out)

    def test_disabled_never_calls_dlp(self):
        fake = FakeDlp()
        r = ModelArmorRedactor(dlp_enabled=False, client=fake)
        self.assertEqual(r.redact_with_path(MSG)[1], PATH_REGEX)
        self.assertEqual(fake.calls, [])
        self.assertIn("disabled", r.dlp_status())

    def test_env_flag(self):
        with mock.patch.dict(os.environ, {"MODEL_ARMOR_DLP_ENABLED": "false"}):
            self.assertFalse(ModelArmorRedactor().dlp_enabled)
        with mock.patch.dict(os.environ, {"MODEL_ARMOR_DLP_ENABLED": "true"}):
            self.assertTrue(ModelArmorRedactor().dlp_enabled)

    def test_client_is_lazy_and_init_failure_is_remembered(self):
        with mock.patch.object(MA, "DLP_AVAILABLE", True), \
             mock.patch.object(MA, "dlp_v2") as dlp_v2:
            dlp_v2.DlpServiceClient.side_effect = RuntimeError("no credentials")
            r = ModelArmorRedactor(dlp_enabled=True)
            dlp_v2.DlpServiceClient.assert_not_called()          # nothing at construction
            with self.assertLogs(MA.logger, "WARNING"):
                self.assertEqual(r.redact_with_path(MSG)[1], PATH_REGEX)
            r.redact_with_path(MSG)
            self.assertEqual(dlp_v2.DlpServiceClient.call_count, 1)
            self.assertIn("init failed", r.dlp_status())

    def test_package_missing(self):
        with mock.patch.object(MA, "DLP_AVAILABLE", False):
            r = ModelArmorRedactor(dlp_enabled=True)
            self.assertIn("not installed", r.dlp_status())
            with self.assertLogs(MA.logger, "WARNING"):
                out, path = r.redact_with_path(MSG)
            self.assertEqual(path, PATH_REGEX)
            self.assertNotIn("jane@example.com", out)

    def test_empty_text(self):
        r = ModelArmorRedactor(dlp_enabled=True, client=FakeDlp())
        self.assertEqual(r.redact(""), "")

    def test_singleton_is_disabled_in_tests(self):
        self.assertFalse(MA.model_armor.dlp_enabled)


if __name__ == "__main__":
    unittest.main()
