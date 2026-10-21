"""Unit tests for the FAQ Assistant FunctionTools, mock order DB, and citation parsing.

All external calls (Discovery Engine) are mocked; no network or credentials needed.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from google.api_core import exceptions as gax_exceptions
from google.auth import exceptions as auth_exceptions

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)


import tests.helpers.no_network  # noqa: F401,E402  (blocks outbound network)
from agents.faq_assistant import tools  # noqa: E402
from agents.faq_assistant.mock_orders import (  # noqa: E402
    MOCK_ORDERS,
    VALID_STATUSES,
    lookup_order,
    normalize_order_id,
)
from backend.clients.stream_parsing import parse_ndjson_stream  # noqa: E402
from backend.security.model_armor import model_armor  # noqa: E402


class TestMockOrders(unittest.TestCase):

    def test_fixture_integrity(self):
        self.assertGreaterEqual(len(MOCK_ORDERS), 35)
        for oid, rec in MOCK_ORDERS.items():
            self.assertRegex(oid, r"^#\d{5}$")
            self.assertIn(rec["status"], VALID_STATUSES)
        # Every status is represented at least once
        self.assertEqual({r["status"] for r in MOCK_ORDERS.values()}, set(VALID_STATUSES))

    def test_fixture_has_no_customer_pii_fields(self):
        forbidden = {"name", "email", "address", "phone", "card", "payment"}
        for rec in MOCK_ORDERS.values():
            for key in rec:
                self.assertFalse(any(f in key.lower() for f in forbidden), key)

    def test_normalize_accepts_common_variants(self):
        for raw in ["#10101", "#10101", "ord-10101", "ORD 10101", "ORD10101", "10101", "  #10101 "]:
            self.assertEqual(normalize_order_id(raw), "#10101", raw)

    def test_normalize_rejects_invalid(self):
        for raw in [None, 10101, "", "ORD-1010", "ORD-101010", "ORD-10101; DROP TABLE", "../ORD-10101",
                    "X" * 100, "ORD-1O101"]:
            self.assertIsNone(normalize_order_id(raw), raw)

    def test_lookup_returns_defensive_copy(self):
        rec = lookup_order("#10201")
        rec["items"].append("tampered")
        self.assertNotIn("tampered", MOCK_ORDERS["#10201"]["items"])


class TestGetOrderStatusTool(unittest.TestCase):

    def test_found(self):
        out = tools.get_order_status("ord 10501")
        self.assertEqual(out["status"], "found")
        self.assertEqual(out["order"]["order_id"], "#10501")
        self.assertEqual(out["order"]["order_status"], "DELAYED")
        self.assertEqual(out["order"]["estimated_delivery"], "2026-10-21")
        self.assertNotIn("status", out["order"])

    def test_not_found(self):
        out = tools.get_order_status("#99999")
        self.assertEqual(out["status"], "not_found")
        self.assertNotIn("order", out)

    def test_invalid(self):
        self.assertEqual(tools.get_order_status("my order")["status"], "invalid_order_id")


def _fake_result(doc_id, question, answer, brand="AppleSupport"):
    doc = tools.discoveryengine.Document(id=doc_id)
    doc.struct_data = {
        "customer_question": question,
        "reference_brand_response_text": answer,
        "brand_author": brand,
    }
    return tools.discoveryengine.SearchResponse.SearchResult(id=doc_id, document=doc)


class TestSearchSupportRecordsTool(unittest.TestCase):

    def setUp(self):
        self.client = MagicMock()
        patcher = patch.object(tools, "_get_search_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_maps_fields(self):
        self.client.search.return_value = iter([
            _fake_result("twcs_1", "airpods disconnect", "Reset them via the case button."),
            _fake_result("twcs_2", "battery drain", "Check Battery settings."),
        ])
        out = tools.search_support_records("AirPods disconnecting after iOS update", top_k=2)
        self.assertEqual(out["status"], "success")
        self.assertEqual([r["doc_id"] for r in out["results"]], ["twcs_1", "twcs_2"])
        self.assertEqual(out["results"][0]["brand_response"], "Reset them via the case button.")
        self.assertEqual(out["results"][0]["brand"], "AppleSupport")

    def test_request_is_clamped_and_targets_datastore(self):
        self.client.search.return_value = iter([])
        tools.search_support_records("  q " * 400, top_k=999)
        req = self.client.search.call_args.kwargs["request"]
        self.assertEqual(req.page_size, tools.MAX_TOP_K)
        self.assertLessEqual(len(req.query), tools.MAX_QUERY_LENGTH)
        self.assertTrue(req.serving_config.endswith("/servingConfigs/default_search"))

    def test_no_results(self):
        self.client.search.return_value = iter([])
        self.assertEqual(tools.search_support_records("unknown topic xyz")["status"], "no_results")

    def test_empty_query_skips_api(self):
        out = tools.search_support_records("   ")
        self.assertEqual(out["status"], "invalid_query")
        self.client.search.assert_not_called()

    def test_rate_limit_is_reported_without_details(self):
        self.client.search.side_effect = gax_exceptions.ResourceExhausted("quota secret-detail")
        out = tools.search_support_records("shipping to Canada")
        self.assertEqual(out, {"status": "error", "error_type": "rate_limited", "results": []})

    def test_other_api_error(self):
        self.client.search.side_effect = gax_exceptions.ServiceUnavailable("down secret-detail")
        out = tools.search_support_records("x y z")
        self.assertEqual(out, {"status": "error", "error_type": "search_unavailable", "results": []})

    def test_unauthenticated_is_auth_error_without_details(self):
        self.client.search.side_effect = gax_exceptions.Unauthenticated("Expected OAuth 2 access token secret-detail")
        out = tools.search_support_records("Newegg return policy")
        self.assertEqual(out, {"status": "error", "error_type": "auth_error", "results": []})

    def test_client_auth_setup_failure_is_auth_error(self):
        with patch.object(tools, "_get_search_client", side_effect=auth_exceptions.MutualTLSChannelError("bad cert")):
            out = tools.search_support_records("Newegg return policy")
        self.assertEqual(out["error_type"], "auth_error")

    def test_permission_denied(self):
        self.client.search.side_effect = gax_exceptions.PermissionDenied("no discoveryengine.servingConfigs.search")
        out = tools.search_support_records("Newegg return policy")
        self.assertEqual(out, {"status": "error", "error_type": "permission_denied", "results": []})


class TestSearchClientAuthMode(unittest.TestCase):
    """_get_search_client must use mTLS + the workload cert whenever Agent Identity provisions one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for var in ("GOOGLE_API_CERTIFICATE_CONFIG", "GOOGLE_API_USE_CLIENT_CERTIFICATE"):
            os.environ.pop(var, None)
        client_patch = patch.object(tools.discoveryengine, "SearchServiceClient")
        self.client_cls = client_patch.start()
        self.addCleanup(client_patch.stop)

    def _write(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _write_cert_config(self):
        cert = self._write("cert.pem", "CERT")
        key = self._write("key.pem", "KEY")
        cfg = self._write("certificate_config.json", json.dumps(
            {"cert_configs": {"workload": {"cert_path": cert, "key_path": key}}}))
        os.environ["GOOGLE_API_CERTIFICATE_CONFIG"] = cfg

    def test_agent_identity_uses_mtls_endpoint_and_workload_cert(self):
        self._write_cert_config()
        tools._get_search_client()
        kwargs = self.client_cls.call_args.kwargs
        opts = kwargs["client_options"]
        self.assertEqual(opts.api_endpoint, tools.MTLS_ENDPOINT)
        self.assertEqual(opts.client_cert_source(), (b"CERT", b"KEY"))
        self.assertEqual(kwargs["transport"], "rest")
        self.assertNotIn("credentials", kwargs)
        self.assertEqual(os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"], "true")

    def test_no_cert_config_uses_regular_endpoint(self):
        tools._get_search_client()
        kwargs = self.client_cls.call_args.kwargs
        self.assertIsNone(getattr(kwargs["client_options"], "api_endpoint", None))
        self.assertIsNone(getattr(kwargs["client_options"], "client_cert_source", None))
        self.assertNotIn("GOOGLE_API_USE_CLIENT_CERTIFICATE", os.environ)

    def test_malformed_cert_config_falls_back(self):
        os.environ["GOOGLE_API_CERTIFICATE_CONFIG"] = self._write("certificate_config.json", "{not json")
        tools._get_search_client()
        self.assertIsNone(getattr(self.client_cls.call_args.kwargs["client_options"], "api_endpoint", None))

    def test_missing_cert_file_falls_back(self):
        os.environ["GOOGLE_API_CERTIFICATE_CONFIG"] = self._write("certificate_config.json", json.dumps(
            {"cert_configs": {"workload": {"cert_path": "/nonexistent/c.pem", "key_path": "/nonexistent/k.pem"}}}))
        tools._get_search_client()
        self.assertIsNone(getattr(self.client_cls.call_args.kwargs["client_options"], "api_endpoint", None))

    def test_explicit_opt_out_is_respected(self):
        self._write_cert_config()
        os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"
        tools._get_search_client()
        self.assertIsNone(getattr(self.client_cls.call_args.kwargs["client_options"], "api_endpoint", None))
        self.assertEqual(os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"], "false")

    def test_search_client_is_reused_across_calls(self):
        c1 = tools._get_search_client()
        c2 = tools._get_search_client()
        self.assertIs(c1, c2)
        self.assertEqual(self.client_cls.call_count, 1)


class TestBackendParsing(unittest.TestCase):

    def test_function_response_citations_and_final_text(self):
        events = [
            {"content": {"parts": [{"text": "Let me check."}, {"function_call": {"name": "get_order_status"}}]}},
            {"content": {"parts": [{"function_response": {
                "name": "get_order_status",
                "response": {"status": "found", "order": {"order_id": "#10201"}}}}]}},
            {"content": {"parts": [{"functionResponse": {
                "name": "search_support_records",
                "response": {"status": "success", "query": "UPS delay",
                             "results": [{"doc_id": "twcs_42"}]}}}]}},
            {"content": {"parts": [{"text": "{\"answer\": \"In transit\", \"citations\": [], "
                                            "\"grounded\": true, \"escalate\": false}"}]}},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        text, citations = parse_ndjson_stream(raw)
        self.assertTrue(text.startswith("{"))  # pre-tool "Let me check." text dropped
        self.assertIn("Order Lookup: #10201", citations)
        self.assertIn("Document #1 (ID: twcs_42)", citations)
        self.assertIn("Datastore Search: UPS delay", citations)


class TestOrderIdNotRedacted(unittest.TestCase):

    def test_order_id_survives_regex_redaction(self):
        text = "Where is order #10201? Email me at a@b.com"
        out = model_armor._regex_redact(text)
        self.assertIn("#10201", out)
        self.assertIn("[EMAIL_REDACTED]", out)


if __name__ == "__main__":
    unittest.main()
