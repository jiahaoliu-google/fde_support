"""Unit tests for the customer-facing answer sanitizer and the agent's after_model_callback."""

import json
import os
import sys
import unittest

from google.adk.models.llm_response import LlmResponse
from google.genai import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)


from agents.faq_assistant.answer_sanitizer import (  # noqa: E402
    sanitize_answer,
    sanitize_response_json_text,
)

LEAK_MARKERS = ("twcs_", "doc_id", "datastore", "data store", "knowledge base", "support records",
                "other users", "search_support_records", "get_order_status", "dataStores/")


def assert_clean(test, text):
    lowered = text.lower()
    for marker in LEAK_MARKERS:
        test.assertNotIn(marker.lower(), lowered, f"leaked {marker!r} in: {text!r}")


class TestSanitizeAnswer(unittest.TestCase):

    def test_real_smoke_test_outputs(self):
        # Verbatim shapes observed from gemini-2.5-pro during the live smoke test.
        cases = {
            "1.  **Restart your iPhone:** This can often resolve temporary software glitches. (doc_id: twcs_236803, twcs_260344)\n2.  **Unpair and re-pair your AirPods.**":
                "1. **Restart your iPhone:** This can often resolve temporary software glitches.\n2. **Unpair and re-pair your AirPods.**",
            "1.  **Restart your iPhone:** This is a simple step. [twcs_236803, twcs_260344]":
                "1. **Restart your iPhone:** This is a simple step.",
            "Based on reports from other users, here are a few things you can try.":
                "Here are a few things you can try.",
            "Based on our support records, here are a few things you can try to resolve the issue.":
                "Here are a few things you can try to resolve the issue.",
            "According to our records, the rebooking options were only for travel up to December 31st.":
                "The rebooking options were only for travel up to December 31st.",
            "First, try restarting your iPhone (doc_id: twcs_236803).":
                "First, try restarting your iPhone.",
        }
        for raw, expected in cases.items():
            out = sanitize_answer(raw)
            self.assertEqual(out, expected)
            assert_clean(self, out)

    def test_other_citation_shapes(self):
        for raw in [
            "Reset the network settings (Source: twcs_1).",
            "Reset the network settings [Sources: twcs_1 and twcs_2].",
            "Reset the network settings (see doc_id: twcs_1).",
            "Reset the network settings. Doc ID: twcs_99",
            "Reset the network settings.\nSources:\n- twcs_1\n- twcs_2",
        ]:
            out = sanitize_answer(raw)
            assert_clean(self, out)
            self.assertTrue(out.startswith("Reset the network settings"), out)

    def test_attribution_clauses_mid_text(self):
        out = sanitize_answer(
            "Sorry about that. According to the knowledge base, you can return items within 30 days. "
            "From the search results: a receipt is required."
        )
        self.assertEqual(out, "Sorry about that. You can return items within 30 days. A receipt is required.")

    def test_residual_backend_terms_and_internals(self):
        out = sanitize_answer(
            "I couldn't find that in our knowledge base. I called search_support_records on "
            "projects/p/locations/global/collections/default_collection/dataStores/ds-v2."
        )
        assert_clean(self, out)
        self.assertIn("our support resources", out)

    def test_legitimate_text_unchanged(self):
        for raw in [
            "Your order #10201 is in transit via UPS, tracking 1Z999AA10123456791, arriving 2026-10-19.",
            "Please keep the warranty documents that came with your device.",
            "In Settings, tap Bluetooth, then tap the (i) icon next to your AirPods.",
            "Could you tell me which brand and product you'd like to return?",
            "Per Apple's policy, returns are accepted within 14 days.",
        ]:
            self.assertEqual(sanitize_answer(raw), raw)

    def test_non_string_passthrough(self):
        self.assertIsNone(sanitize_answer(None))
        self.assertEqual(sanitize_answer(""), "")


class TestSanitizeJsonText(unittest.TestCase):

    def test_answer_cleaned_citations_kept(self):
        raw = json.dumps({
            "answer": "Restart your iPhone. (doc_id: twcs_236803)",
            "citations": ["twcs_236803", "twcs_260344"],
            "grounded": True,
            "escalate": False,
        })
        data = json.loads(sanitize_response_json_text(raw))
        self.assertEqual(data["answer"], "Restart your iPhone.")
        self.assertEqual(data["citations"], ["twcs_236803", "twcs_260344"])  # kept for debugging
        self.assertTrue(data["grounded"])

    def test_clean_json_returned_unchanged(self):
        raw = '{"answer": "Hello", "citations": [], "grounded": false, "escalate": false}'
        self.assertIs(sanitize_response_json_text(raw), raw)

    def test_plain_text(self):
        self.assertEqual(sanitize_response_json_text("Try this [twcs_1]."), "Try this.")


class TestAgentCallback(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from agents.faq_assistant.agent import faq_assistant_agent, sanitize_model_response
        cls.agent = faq_assistant_agent
        cls.callback = staticmethod(sanitize_model_response)

    def test_callback_registered(self):
        self.assertIs(self.agent.after_model_callback, self.callback)

    def test_final_json_text_sanitized(self):
        payload = {"answer": "Restart it [twcs_1].", "citations": ["twcs_1"], "grounded": True, "escalate": False}
        resp = LlmResponse(content=types.Content(role="model", parts=[types.Part(text=json.dumps(payload))]))
        out = self.callback(None, resp)
        data = json.loads(out.content.parts[0].text)
        self.assertEqual(data["answer"], "Restart it.")
        self.assertEqual(data["citations"], ["twcs_1"])

    def test_set_model_response_args_sanitized(self):
        fc = types.FunctionCall(name="set_model_response",
                                args={"answer": "Restart it (doc_id: twcs_1).", "citations": ["twcs_1"],
                                      "grounded": True, "escalate": False})
        resp = LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=fc)]))
        out = self.callback(None, resp)
        self.assertEqual(out.content.parts[0].function_call.args["answer"], "Restart it.")
        self.assertEqual(out.content.parts[0].function_call.args["citations"], ["twcs_1"])

    def test_tool_calls_and_clean_text_untouched(self):
        fc = types.FunctionCall(name="search_support_records", args={"query": "twcs_ style query"})
        resp = LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=fc)]))
        self.assertIsNone(self.callback(None, resp))


if __name__ == "__main__":
    unittest.main()
