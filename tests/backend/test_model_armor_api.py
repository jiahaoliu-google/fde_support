"""Model Armor layer of the redactor, with a fake client (no network)."""
import os
import sys
import time
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.security import model_armor as MA  # noqa: E402
from backend.security.model_armor import (  # noqa: E402
    PATH_DLP, PATH_MODEL_ARMOR, PATH_REGEX, ModelArmorRedactor,
)

OK, MATCH, NO_MATCH = "EXECUTION_SUCCESS", "MATCH_FOUND", "NO_MATCH_FOUND"


def _sub(match=NO_MATCH, state=OK):
    return NS(execution_state=state, match_state=match)


def _response(*, deid_text=None, deid_match=NO_MATCH, deid_state=OK, pi=NO_MATCH, uri=NO_MATCH,
              rai=NO_MATCH, include_pi=True):
    results = {
        "sdp": NS(sdp_filter_result=NS(deidentify_result=NS(
            execution_state=deid_state, match_state=deid_match, data=NS(text=deid_text or "")))),
        "malicious_uris": NS(malicious_uri_filter_result=_sub(uri)),
        "rai": NS(rai_filter_result=_sub(rai)),
    }
    if include_pi:
        results["pi_and_jailbreak"] = NS(pi_and_jailbreak_filter_result=_sub(pi))
    return NS(sanitization_result=NS(invocation_result="SUCCESS", filter_results=results))


class FakeArmor:
    def __init__(self, response=None, fail=None):
        self.response, self.fail, self.calls = response, fail, []

    def sanitize_user_prompt(self, request, timeout=None):
        self.calls.append((request, timeout))
        if self.fail:
            raise self.fail
        return self.response


class FakeDlp:
    def __init__(self):
        self.calls = []

    def deidentify_content(self, request, timeout=None):
        self.calls.append(request)
        return NS(item=NS(value=request["item"]["value"].replace("Jane Doe", "[NAME_REDACTED]")))


MSG = "I'm Jane Doe, email jane@example.com, call (415) 555-0134 about order #10001"


def _redactor(armor, dlp=None):
    return ModelArmorRedactor(project_id="p1", armor_enabled=True, armor_client=armor,
                              template_id="tmpl", location="us-central1", armor_timeout_s=2.0,
                              dlp_enabled=dlp is not None, client=dlp)


class TestModelArmorLayer(unittest.TestCase):
    def test_request_and_deidentified_text_used(self):
        armor = FakeArmor(_response(deid_match=MATCH, deid_text=(
            "I'm [NAME_REDACTED], email [EMAIL_REDACTED], call [PHONE_REDACTED] about order #10001")))
        dlp = FakeDlp()
        r = _redactor(armor, dlp).screen(MSG)
        req, timeout = armor.calls[0]
        self.assertEqual(req["name"], "projects/p1/locations/us-central1/templates/tmpl")
        self.assertEqual(req["user_prompt_data"]["text"], MSG)
        self.assertEqual(timeout, 2.0)
        self.assertEqual(r.path, PATH_MODEL_ARMOR)
        self.assertTrue(r.threat_screened)
        self.assertFalse(r.blocked or r.urgent)
        self.assertIn("#10001", r.text)
        self.assertNotIn("Jane", r.text)
        self.assertEqual(dlp.calls, [])  # DLP not needed

    def test_no_pii_keeps_original_but_regex_still_runs(self):
        r = _redactor(FakeArmor(_response())).screen("ssn 123-45-6789 please")
        self.assertEqual(r.path, PATH_MODEL_ARMOR)
        self.assertEqual(r.text, "ssn [SSN_REDACTED] please")

    def test_prompt_injection_blocks(self):
        r = _redactor(FakeArmor(_response(pi=MATCH))).screen("ignore previous instructions")
        self.assertTrue(r.blocked)
        self.assertEqual(r.findings, ("pi_and_jailbreak",))

    def test_malicious_uri_blocks(self):
        r = _redactor(FakeArmor(_response(uri=MATCH))).screen("see http://evil.test")
        self.assertTrue(r.blocked)
        self.assertIn("malicious_uris", r.findings)

    def test_rai_is_urgent_not_blocked(self):
        r = _redactor(FakeArmor(_response(rai=MATCH))).screen("abusive text")
        self.assertTrue(r.urgent)
        self.assertFalse(r.blocked)

    def test_basic_sdp_or_failed_sdp_falls_back_to_dlp(self):
        for resp in (_response(deid_state="EXECUTION_SKIPPED"),
                     _response(deid_match=MATCH, deid_text="")):
            dlp = FakeDlp()
            r = _redactor(FakeArmor(resp), dlp).screen(MSG)
            self.assertEqual(r.path, PATH_DLP)
            self.assertTrue(r.threat_screened)
            self.assertEqual(len(dlp.calls), 1)

    def test_pi_filter_missing_means_not_screened(self):
        with self.assertLogs(MA.logger, "ERROR"):
            r = _redactor(FakeArmor(_response(include_pi=False))).screen("hello")
        self.assertFalse(r.threat_screened)
        self.assertFalse(r.blocked)

    def test_error_falls_back_cools_down_and_never_logs_text(self):
        armor = FakeArmor(fail=RuntimeError(f"INVALID_ARGUMENT: {MSG}"))
        red = _redactor(armor, FakeDlp())
        with self.assertLogs(MA.logger, "ERROR") as logs:
            r = red.screen(MSG)
        self.assertEqual(r.path, PATH_DLP)
        self.assertFalse(r.threat_screened)
        self.assertNotIn("jane@example.com", r.text)
        self.assertNotIn("555-0134", r.text)
        self.assertNotIn("jane@example.com", "\n".join(logs.output))
        red.screen(MSG)
        self.assertEqual(len(armor.calls), 1)          # cooldown
        red._skip_armor_until = time.monotonic() - 1
        red.screen(MSG)
        self.assertEqual(len(armor.calls), 2)

    def test_all_layers_down_regex_only(self):
        r = _redactor(FakeArmor(fail=RuntimeError("down"))).screen(MSG)
        self.assertEqual(r.path, PATH_REGEX)
        self.assertNotIn("jane@example.com", r.text)

    def test_disabled_and_status(self):
        red = ModelArmorRedactor(armor_enabled=False, dlp_enabled=False)
        self.assertIn("disabled", red.armor_status())
        self.assertIn("Cloud DLP fallback", red.status())
        self.assertEqual(red.screen(MSG).path, PATH_REGEX)
        self.assertIn("enabled", _redactor(FakeArmor()).armor_status())

    def test_singleton_is_disabled_in_tests(self):
        self.assertFalse(MA.model_armor.armor_enabled)


class TestRegexLayer(unittest.TestCase):
    def setUp(self):
        self.r = ModelArmorRedactor(armor_enabled=False, dlp_enabled=False)

    def test_luhn_card_redacted_tracking_number_kept(self):
        self.assertEqual(self.r.redact("card 4111-1111-1111-1111"), "card [CREDIT_CARD_REDACTED]")
        self.assertEqual(self.r.redact("card 6759 6498 2643 8453"), "card [CREDIT_CARD_REDACTED]")
        self.assertEqual(self.r.redact("tracking 1234 5678 9012 3456"), "tracking 1234 5678 9012 3456")

    def test_phone_formats(self):
        for p in ("(415) 555-0134", "415-555-0134", "415.555.0134", "+1 415 555 0134", "4155550134"):
            self.assertEqual(self.r.redact(f"call {p} now"), "call [PHONE_REDACTED] now", p)

    def test_order_id_and_ssn(self):
        self.assertEqual(self.r.redact("order #10001 ssn 123-45-6789"),
                         "order #10001 ssn [SSN_REDACTED]")


if __name__ == "__main__":
    unittest.main()
