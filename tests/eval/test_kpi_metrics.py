"""Unit tests for the pre-deployment eval: KPI math, the inference harness (with fake
ADK runners, no network) and the KPI report generator.

Run with:  python3 -m unittest tests/eval/test_kpi_metrics.py -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO_ROOT)

import kpi_metrics as km  # noqa: E402
import run_inference as ri  # noqa: E402
from backend_probe import BackendProbe, OP_AGENT_REPLY, OP_FALLBACK, OP_SYNC_TICKET  # noqa: E402
from scripts.eval import generate_kpi_report as report  # noqa: E402


U, N = km.URGENT, km.NEUTRAL
PRICING = {
    "models": {
        "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.03},
        "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cached_input": 0.125},
        "unpriced-model": {"input": None, "output": None, "cached_input": None},
    },
    "search": {"edition": "standard", "per_1k_queries": {"standard": 1.50, "enterprise": 4.00}},
}
MODELS = {"sentiment": "gemini-2.5-flash", "faq": "gemini-2.5-pro"}


# ---------------------------------------------------------------------------
# kpi_metrics
# ---------------------------------------------------------------------------
class TestClassificationReport(unittest.TestCase):

    def test_perfect_routing(self):
        r = km.classification_report([(U, U)] * 25 + [(N, N)] * 75)
        self.assertEqual(r["accuracy"], 1.0)
        self.assertEqual(r["per_class"][U]["f1"], 1.0)

    def test_hand_worked_example(self):
        # 25 urgent: 22 caught, 2 called neutral, 1 errored. 75 neutral: 3 called urgent.
        pairs = [(U, U)] * 22 + [(U, N)] * 2 + [(U, km.ERROR)] + [(N, U)] * 3 + [(N, N)] * 72
        r = km.classification_report(pairs)
        self.assertAlmostEqual(r["accuracy"], 94 / 100)
        urgent = r["per_class"][U]
        self.assertEqual((urgent["tp"], urgent["fp"], urgent["fn"]), (22, 3, 3))
        self.assertAlmostEqual(urgent["precision"], 22 / 25)
        self.assertAlmostEqual(urgent["recall"], 22 / 25)
        self.assertAlmostEqual(urgent["f1"], 0.88)
        self.assertEqual(r["error_predictions"], 1)
        self.assertEqual(r["confusion"][U], {U: 22, N: 2, km.ERROR: 1})

    def test_f1_undefined_without_urgent_cases(self):
        # The old report claimed Urgent F1 = 1.00 on a set with no urgent cases.
        r = km.classification_report([(N, N)] * 5)
        self.assertIsNone(r["per_class"][U]["f1"])
        self.assertIsNone(km.gate(r["per_class"][U]["f1"], 0.95))

    def test_empty(self):
        self.assertIsNone(km.classification_report([])["accuracy"])


class TestLatencyAndCost(unittest.TestCase):

    def test_percentile_matches_numpy_linear(self):
        data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertAlmostEqual(km.percentile(data, 95), 9.55)
        self.assertAlmostEqual(km.percentile(data, 50), 5.5)
        self.assertEqual(km.percentile([2.0], 95), 2.0)
        self.assertIsNone(km.percentile([], 95))

    def test_latency_summary_skips_none(self):
        s = km.latency_summary([1.0, None, 3.0])
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["max"], 3.0)
        self.assertIsNone(km.latency_summary([None]))

    def test_cost_matches_evaluation_plan_example(self):
        # docs/EVALUATION_PLAN.md: (338*1.25 + 512*0.125 + 120*10)/1M + one search = $0.003187
        usage = {"prompt_token_count": 850, "cached_content_token_count": 512,
                 "candidates_token_count": 120, "thoughts_token_count": 0}
        cost = km.model_call_cost("gemini-2.5-pro", usage, PRICING) + km.search_cost(1, PRICING)
        self.assertAlmostEqual(cost, 0.0031865, places=7)

    def test_thinking_tokens_bill_as_output(self):
        usage = {"prompt_token_count": 0, "cached_content_token_count": 0,
                 "candidates_token_count": 0, "thoughts_token_count": 1_000_000}
        self.assertAlmostEqual(km.model_call_cost("gemini-2.5-flash", usage, PRICING), 2.50)

    def test_unknown_price_is_none_not_zero(self):
        usage = km.empty_usage()
        self.assertIsNone(km.model_call_cost("unpriced-model", usage, PRICING))
        self.assertIsNone(km.model_call_cost("not-in-file", usage, PRICING))
        self.assertIsNone(km.search_cost(1, {"search": {"edition": "x", "per_1k_queries": {}}}))

    def test_repo_pricing_file_loads(self):
        p = km.load_pricing()
        self.assertIsNotNone(km.model_call_cost("gemini-2.5-flash", km.empty_usage(), p))
        self.assertIsNotNone(km.model_call_cost("gemini-2.5-pro", km.empty_usage(), p))

    def test_gate_direction(self):
        self.assertTrue(km.gate(2.0, 3.0, higher_is_better=False))
        self.assertFalse(km.gate(0.01, 0.0, higher_is_better=False))
        self.assertTrue(km.gate(0.95, 0.95))

    def test_only_tdd_targets(self):
        # Anything without a TDD target must not have one here (no assumed targets).
        self.assertEqual(km.KPI_TARGETS, {"routing_accuracy": 0.95, "urgent_f1": 0.95, "db_write_p95_ms": 100.0})


# ---------------------------------------------------------------------------
# Inference harness with fake ADK runners
# ---------------------------------------------------------------------------
def usage(prompt, out, cached=0, thoughts=0):
    return {"prompt_token_count": prompt, "cached_content_token_count": cached,
            "candidates_token_count": out, "thoughts_token_count": thoughts, "total_token_count": prompt + out}


def text_event(text, u=None):
    return NS(partial=False, usage_metadata=u, content=NS(parts=[NS(text=text)]))


def call_event(name, args, cid, u=None):
    return NS(partial=False, usage_metadata=u,
              content=NS(parts=[NS(function_call=NS(name=name, args=args, id=cid))]))


def response_event(name, response, cid):
    return NS(partial=False, usage_metadata=None,
              content=NS(parts=[NS(function_response=NS(name=name, response=response, id=cid))]))


class FakeRunner:
    """Stands in for ADK Runner.run_async; `script(message)` returns the events to yield."""

    def __init__(self, script, raise_exc=None):
        self.script, self.raise_exc, self.calls = script, raise_exc, []

    async def run_async(self, user_id, session_id, new_message):
        msg = new_message.parts[0].text
        self.calls.append((session_id, msg))
        if self.raise_exc:
            raise self.raise_exc
        for e in self.script(msg):
            yield e


class FakeClock:
    """Each call advances 0.5 s, so latencies are deterministic."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.5
        return self.t


def sentiment_script(label_for):
    return lambda msg: [text_event(json.dumps({"sentiment": label_for(msg)}), usage(100, 20))]


def faq_script(msg):
    return [
        call_event("search_support_records", {"query": msg}, "c1", usage(800, 30)),
        response_event("search_support_records", {"status": "success", "results": [{"doc_id": "twcs_1"}]}, "c1"),
        text_event(json.dumps({"answer": "Do X.", "citations": ["twcs_1"], "grounded": True, "escalate": False}),
                   usage(1200, 60, cached=500, thoughts=40)),
    ]


def golden(cid, segment, expected, text="hello", turns=None):
    case = {"eval_case_id": cid, "segment": segment, "expected_sentiment": expected,
            "expected_route": "HUMAN_QUEUE" if expected == U else "FAQ_AGENT"}
    if turns:
        case["agent_data"] = {"turns": [{"turn_index": i + 1, "prompt": {"parts": [{"text": t}]}}
                                        for i, t in enumerate(turns)]}
    else:
        case["prompt"] = {"role": "user", "parts": [{"text": text}]}
    return case


def run(coro):
    return asyncio.run(coro)


class TestRunAgentTurn(unittest.TestCase):

    def test_measures_tokens_search_latency_and_text(self):
        t = run(ri.run_agent_turn(FakeRunner(faq_script), "gemini-2.5-pro", "s1", "q", 10, clock=FakeClock()))
        self.assertIsNone(t["error"])
        self.assertEqual(t["llm_calls"], 2)
        self.assertEqual(t["usage"]["prompt_token_count"], 2000)
        self.assertEqual(t["usage"]["thoughts_token_count"], 40)
        self.assertEqual(len(t["search_calls"]), 1)
        s = t["search_calls"][0]
        self.assertEqual((s["status"], s["doc_ids"]), ("success", ["twcs_1"]))
        self.assertAlmostEqual(s["latency_s"], 0.5)   # function_call -> function_response events
        self.assertAlmostEqual(t["latency_s"], 2.0)   # start + 3 events + end, 0.5 s apart
        self.assertIn("Do X.", t["final_text"])

    def test_exception_is_recorded_not_raised(self):
        exc = RuntimeError("429 RESOURCE_EXHAUSTED")
        t = run(ri.run_agent_turn(FakeRunner(None, raise_exc=exc), "m", "s", "q", 10))
        self.assertEqual(t["error_type"], "rate_limited")

    def test_timeout_is_recorded(self):
        class Hang:
            async def run_async(self, **_):
                await asyncio.sleep(5)
                yield None
        t = run(ri.run_agent_turn(Hang(), "m", "s", "q", 0.01))
        self.assertEqual(t["error_type"], "timeout")

    def test_search_tool_error_captured(self):
        script = lambda m: [call_event("search_support_records", {"query": m}, "c", usage(10, 1)),  # noqa: E731
                            response_event("search_support_records",
                                           {"status": "error", "error_type": "rate_limited"}, "c"),
                            text_event('{"answer": "sorry", "escalate": true}', usage(10, 1))]
        t = run(ri.run_agent_turn(FakeRunner(script), "m", "s", "q", 10))
        self.assertEqual(t["search_calls"][0]["error_type"], "rate_limited")


class TestRunCase(unittest.TestCase):

    def test_urgent_case_skips_faq(self):
        faq = FakeRunner(faq_script)
        r = run(ri.run_case(golden("U-1", "U", U), FakeRunner(sentiment_script(lambda m: U)), faq,
                            MODELS, PRICING, 10))
        self.assertEqual(r["predicted_sentiment"], U)
        self.assertEqual(faq.calls, [])
        self.assertAlmostEqual(r["cost_usd"], (100 * 0.30 + 20 * 2.50) / 1e6)

    def test_neutral_case_runs_faq_and_prices_search(self):
        r = run(ri.run_case(golden("S-1", "S", N), FakeRunner(sentiment_script(lambda m: N)),
                            FakeRunner(faq_script), MODELS, PRICING, 10))
        t = r["turns"][0]
        self.assertTrue(t["faq"]["output_valid"])
        expected = ((100 * 0.30 + 20 * 2.50)
                    + ((2000 - 500) * 1.25 + 500 * 0.125 + (90 + 40) * 10.0)) / 1e6 + 0.0015
        self.assertAlmostEqual(r["cost_usd"], expected)

    def test_multi_turn_reuses_faq_session_with_fresh_sentiment_sessions(self):
        sent, faq = FakeRunner(sentiment_script(lambda m: N)), FakeRunner(faq_script)
        run(ri.run_case(golden("M-1", "M", N, turns=["help", "Spotify: why?"]), sent, faq, MODELS, PRICING, 10))
        self.assertEqual([m for _, m in faq.calls], ["help", "Spotify: why?"])
        self.assertEqual(len({s for s, _ in faq.calls}), 1)
        self.assertEqual(len({s for s, _ in sent.calls}), 2)

    def test_urgent_second_turn_escalates_and_stops(self):
        sent = FakeRunner(sentiment_script(lambda m: U if "!!" in m else N))
        faq = FakeRunner(faq_script)
        r = run(ri.run_case(golden("M-2", "M", N, turns=["hi", "FIX IT!!", "never sent"]), sent, faq,
                            MODELS, PRICING, 10))
        self.assertEqual(r["predicted_sentiment"], U)
        self.assertEqual(len(r["turns"]), 2)
        self.assertEqual(len(faq.calls), 1)

    def test_sentiment_failure_fails_closed(self):
        r = run(ri.run_case(golden("S-2", "S", N), FakeRunner(None, raise_exc=TimeoutError()),
                            FakeRunner(faq_script), MODELS, PRICING, 10))
        self.assertEqual(r["predicted_sentiment"], km.ERROR)
        self.assertEqual(r["predicted_route"], "HUMAN_QUEUE")
        self.assertTrue(r["errored"])

    def test_invalid_label(self):
        r = run(ri.run_case(golden("S-3", "S", N), FakeRunner(sentiment_script(lambda m: "ANGRY")),
                            None, MODELS, PRICING, 10))
        self.assertEqual(r["predicted_sentiment"], km.INVALID)

    def test_unpriced_model_gives_no_cost(self):
        r = run(ri.run_case(golden("U-2", "U", U), FakeRunner(sentiment_script(lambda m: U)), None,
                            {"sentiment": "unpriced-model"}, PRICING, 10))
        self.assertIsNone(r["cost_usd"])
        self.assertIn("unpriced-model", r["cost_unpriced_reason"])

    def test_sessions_created_before_running(self):
        """Regression: CI's ADK had no Runner(auto_create_session=...). Sessions must exist
        before run_async, whether create_session is sync (ADK 0.x) or async (>=1.0)."""
        for is_async in (True, False):
            created = []

            class Svc:
                def create_session(self, app_name, user_id, session_id):
                    created.append((app_name, session_id))
                    if is_async:
                        async def done():
                            return None
                        return done()

            class SessionCheckingRunner(FakeRunner):
                def __init__(self, script, app_name):
                    super().__init__(script)
                    self.session_service, self.app_name = Svc(), app_name

                async def run_async(self, user_id, session_id, new_message):
                    assert (self.app_name, session_id) in created, "session not created"
                    async for e in super().run_async(user_id, session_id, new_message):
                        yield e

            sent = SessionCheckingRunner(sentiment_script(lambda m: N), "s")
            faq = SessionCheckingRunner(faq_script, "f")
            r = run(ri.run_case(golden("M-3", "M", N, turns=["a", "b"]), sent, faq, MODELS, PRICING, 10))
            self.assertFalse(r["errored"], r)
            self.assertEqual(sum(1 for a, _ in created if a == "s"), 2)   # one per message
            self.assertEqual(sum(1 for a, _ in created if a == "f"), 1)   # one per conversation


def _results():
    cases = [golden(f"U-{i}", "U", U) for i in range(4)] + [golden(f"S-{i}", "S", N) for i in range(6)]
    # U-3 is missed (classified neutral); S-5 is a false alarm.
    sent = FakeRunner(sentiment_script(lambda m: m))
    for c in cases:
        c["prompt"]["parts"][0]["text"] = U if c["eval_case_id"] in ("U-0", "U-1", "U-2", "S-5") else N
    return run(ri.run_all(cases, sent, FakeRunner(faq_script), MODELS, PRICING, 3, 10))


class TestSummarize(unittest.TestCase):

    def setUp(self):
        self.results = _results()
        self.s = ri.summarize(self.results, PRICING, {"suite": "full"})

    def test_routing(self):
        self.assertAlmostEqual(self.s["routing"]["accuracy"], 0.8)
        self.assertAlmostEqual(self.s["routing"]["per_class"][U]["f1"], 0.75)
        self.assertFalse(self.s["gates"]["routing_accuracy"])
        self.assertEqual({m["eval_case_id"] for m in self.s["misrouted"]}, {"U-3", "S-5"})

    def test_only_tdd_gates(self):
        self.assertEqual(set(self.s["gates"]), {"routing_accuracy", "urgent_f1", "db_write_p95_ms"})
        self.assertIsNone(self.s["gates"]["db_write_p95_ms"])          # no probe: T3 not measured

    def test_untargeted_metrics_present(self):
        lat = self.s["latency"]
        self.assertEqual(lat["sentiment_agent_s"]["n"], 10)
        self.assertEqual(lat["faq_agent_s"]["n"], 6)                  # 6 tickets routed to FAQ
        self.assertIsNone(lat["model_armor_redaction_ms"])             # no probe
        self.assertEqual(self.s["errors"]["search_calls"], 6)
        self.assertAlmostEqual(self.s["cost"]["per_1k_tickets_usd"],
                               sum(r["cost_usd"] for r in self.results) / 10 * 1000)
        self.assertEqual(set(self.s["cost"]["by_segment"]), {"U", "S"})

    def test_agent_errors_counted_without_a_gate(self):
        bad = run(ri.run_case(golden("S-9", "S", N), FakeRunner(None, raise_exc=RuntimeError("503")),
                              None, MODELS, PRICING, 10))
        s = ri.summarize(self.results + [bad], PRICING, {"suite": "full"})
        self.assertEqual(s["errors"]["agent_call_errors"], 1)
        self.assertEqual(s["errors"]["by_type"], {"server_error": 1})
        self.assertNotIn("error_rate", s["gates"])


# ---------------------------------------------------------------------------
# Backend probe: Model Armor redaction and T3 ticket writes on a temp SQLite DB
# ---------------------------------------------------------------------------
PII = "my email is jane@example.com"


class TestBackendProbe(unittest.TestCase):

    def setUp(self):
        self.probe = BackendProbe()

    def tearDown(self):
        self.probe.close()

    def _ops(self, r):
        return [[w["op"] for w in t["db_writes"]] for t in r["turns"]]

    def test_neutral_turn_redacts_then_writes_ticket_and_reply(self):
        sent, faq = FakeRunner(sentiment_script(lambda m: N)), FakeRunner(faq_script)
        r = run(ri.run_case(golden("S-1", "S", N, text=PII), sent, faq, MODELS, PRICING, 10, self.probe))
        t = r["turns"][0]
        self.assertTrue(t["redaction"]["changed"])
        self.assertGreaterEqual(t["redaction"]["latency_ms"], 0.0)
        self.assertNotIn("jane@example.com", sent.calls[0][1])        # agents get the redacted text
        self.assertNotIn("jane@example.com", faq.calls[0][1])
        self.assertEqual(self._ops(r), [[OP_SYNC_TICKET, OP_AGENT_REPLY]])
        self.assertTrue(all(w["error"] is None and w["latency_ms"] >= 0 for w in t["db_writes"]))

    def test_urgent_turn_writes_ticket_only(self):
        r = run(ri.run_case(golden("U-1", "U", U), FakeRunner(sentiment_script(lambda m: U)),
                            FakeRunner(faq_script), MODELS, PRICING, 10, self.probe))
        self.assertEqual(self._ops(r), [[OP_SYNC_TICKET]])

    def test_faq_failure_writes_outage_fallback(self):
        r = run(ri.run_case(golden("S-2", "S", N), FakeRunner(sentiment_script(lambda m: N)),
                            FakeRunner(None, raise_exc=RuntimeError("503")), MODELS, PRICING, 10, self.probe))
        self.assertEqual(self._ops(r), [[OP_SYNC_TICKET, OP_FALLBACK]])

    def test_multi_turn_reuses_the_ticket(self):
        sent = FakeRunner(sentiment_script(lambda m: U if "!!" in m else N))
        r = run(ri.run_case(golden("M-1", "M", N, turns=["hi", "FIX IT!!"]), sent, FakeRunner(faq_script),
                            MODELS, PRICING, 10, self.probe))
        self.assertEqual(self._ops(r), [[OP_SYNC_TICKET, OP_AGENT_REPLY], [OP_SYNC_TICKET]])
        self.assertTrue(all(w["error"] is None for t in r["turns"] for w in t["db_writes"]))

    def test_concurrent_cases_on_sqlite_do_not_fail(self):
        cases = [golden(f"S-{i}", "S", N) for i in range(6)]
        results = run(ri.run_all(cases, FakeRunner(sentiment_script(lambda m: N)), FakeRunner(faq_script),
                                 MODELS, PRICING, 3, 10, self.probe))
        s = ri.summarize(results, PRICING, {"suite": "full", "db": self.probe.info()})
        self.assertEqual((s["errors"]["db_writes"], s["errors"]["db_write_errors"]), (12, 0))
        self.assertEqual(s["latency"]["db_write_ms"]["n"], 12)
        self.assertEqual(s["latency"]["model_armor_redaction_ms"]["n"], 6)
        self.assertEqual(s["model_armor"]["path"], self.probe.model_armor_path)
        self.assertFalse(s["db"]["t3_gated"])                         # SQLite: measured, not judged
        self.assertIsNone(s["gates"]["db_write_p95_ms"])

    def test_url_never_contains_password(self):
        self.assertNotIn("secret", BackendProbe.__init__.__doc__ or "")
        from sqlalchemy.engine import make_url
        url = make_url("postgresql+psycopg2://u:secret@127.0.0.1:5432/db")
        self.assertNotIn("secret", url.render_as_string(hide_password=True))


class TestBackendProbeExternalDb(unittest.TestCase):

    def test_rows_of_this_run_are_deleted_on_close(self):
        from sqlalchemy import create_engine, text
        with tempfile.TemporaryDirectory() as d:
            url = f"sqlite:///{os.path.join(d, 'shared.db')}"
            probe = BackendProbe(url)
            run(ri.run_case(golden("S-1", "S", N), FakeRunner(sentiment_script(lambda m: N)),
                            FakeRunner(faq_script), MODELS, PRICING, 10, probe))
            customer = probe.customer_id
            probe.close()
            engine = create_engine(url)
            with engine.connect() as c:
                n_tickets = c.execute(text("SELECT COUNT(*) FROM tickets WHERE customer_id = :c"),
                                      {"c": customer}).scalar()
                n_messages = c.execute(text("SELECT COUNT(*) FROM messages")).scalar()
            engine.dispose()
            self.assertEqual((n_tickets, n_messages), (0, 0))


def _with_db_writes(results, latencies_ms, dialect):
    """Copies results and sets one db write per turn with the given latencies."""
    results = json.loads(json.dumps(results))
    turns = [t for r in results for t in r["turns"]]
    for t, ms in zip(turns, latencies_ms):
        t["db_writes"] = [{"op": OP_SYNC_TICKET, "latency_ms": ms, "error": None}]
    return ri.summarize(results, PRICING, {"suite": "full", "models": MODELS,
                                           "db": {"dialect": dialect, "url": f"{dialect}://x"}})


class TestT3Gate(unittest.TestCase):

    def test_postgres_is_judged_against_100ms(self):
        res = _results()
        fast = _with_db_writes(res, [20.0] * 10, "postgresql")
        self.assertTrue(fast["db"]["t3_gated"])
        self.assertTrue(fast["gates"]["db_write_p95_ms"])
        slow = _with_db_writes(res, [20.0] * 8 + [400.0] * 2, "postgresql")
        self.assertGreater(slow["latency"]["db_write_ms"]["p95"], 100)
        self.assertFalse(slow["gates"]["db_write_p95_ms"])

    def test_sqlite_is_never_judged(self):
        s = _with_db_writes(_results(), [400.0] * 10, "sqlite")
        self.assertIsNone(s["gates"]["db_write_p95_ms"])
        self.assertEqual(s["latency"]["db_write_ms"]["p95"], 400.0)


# ---------------------------------------------------------------------------
# KPI report
# ---------------------------------------------------------------------------
def _grade(recall=0.8):
    metrics = {"grounding_key_fact_recall": recall, "rag_faithfulness": 1.0, "rubric_scope_check": 1.0,
               "case_rubric_adherence": 0.9, "hallucination_v1": 1.0}
    return {
        "summary_metrics": [{"metric_name": k, "mean_score": v, "num_cases_valid": 5, "num_cases_error": 0}
                            for k, v in metrics.items()],
        "eval_case_results": [{"eval_case_index": 0, "response_candidate_results": [{"metric_results": {
            "grounding_key_fact_recall": {"score": 0.0, "explanation": "missed the baggage fact"}}}]}],
    }


class TestReport(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def _kpi(self, data, kid):
        return next(k for k in data["quality_kpis"] if k["id"] == kid)

    def test_kpi_rows_are_exactly_t1_and_t3_to_t8(self):
        _, data = report.build_report(self._write("g.json", _grade()), None, None)
        self.assertEqual([k["id"] for k in data["quality_kpis"]], ["T1", "T3", "T4", "T5", "T6", "T7", "T8"])

    def test_without_router_run_routing_is_not_measured(self):
        md, data = report.build_report(self._write("g.json", _grade()), None, None)
        for kid in ("T1", "T3", "T5"):
            self.assertEqual(self._kpi(data, kid)["status"], report.NOT_MEASURED)
        other = data["no_target_metrics"]
        self.assertTrue(all(r["status"] == report.NOT_MEASURED for r in other["latency"] + other["cost"]))
        self.assertEqual(self._kpi(data, "T1")["measured"], "—")
        for stale in ("1.84 s", "0.62 s", "$3.18", "0.5153"):
            self.assertNotIn(stale, md)

    def test_recall_80_percent_fails_the_85_percent_target(self):
        _, data = report.build_report(self._write("g.json", _grade(0.8)), None, None)
        self.assertEqual(self._kpi(data, "T4")["status"], report.FAIL)
        _, data = report.build_report(self._write("g2.json", _grade(0.86)), None, None)
        self.assertEqual(self._kpi(data, "T4")["status"], report.PASS)

    def test_values_come_from_system_metrics(self):
        s = ri.summarize(_results(), PRICING, {"suite": "full", "models": MODELS})
        md, data = report.build_report(None, None, self._write("system_metrics.json", s))
        self.assertEqual(self._kpi(data, "T1")["measured"], "80.0%")
        self.assertEqual(self._kpi(data, "T1")["status"], report.FAIL)
        self.assertEqual(self._kpi(data, "T5")["measured"], "0.750")
        self.assertIn("T1 Routing Accuracy", data["gates_failed"])
        self.assertIn("`U-3`", md)
        self.assertIn("Routing Detail", md)
        cost_row = data["no_target_metrics"]["cost"][0]
        self.assertEqual(cost_row["measured"], f"${s['cost']['per_1k_tickets_usd']:.2f}")

    def test_untargeted_rows_never_have_a_target_or_fail(self):
        s = _with_db_writes(_results(), [20.0] * 10, "postgresql")
        _, data = report.build_report(self._write("g.json", _grade()), None, self._write("system_metrics.json", s))
        other = data["no_target_metrics"]
        rows = other["latency"] + other["llm_judge"] + other["cost"]
        self.assertEqual({r["target"] for r in rows}, {report.NO_TARGET})
        self.assertNotIn(report.FAIL, {r["status"] for r in rows})
        self.assertEqual([r["metric"] for r in other["llm_judge"]],
                         ["rubric_scope_check", "case_rubric_adherence", "hallucination"])
        self.assertEqual([r["key"] for r in other["latency"]],
                         ["sentiment_agent_s", "faq_agent_s", "model_armor_redaction_ms"])

    def test_t3_judged_on_postgres_info_on_sqlite(self):
        res = _results()
        pg = _with_db_writes(res, [20.0] * 10, "postgresql")
        _, data = report.build_report(None, None, self._write("pg.json", pg))
        self.assertEqual(self._kpi(data, "T3")["status"], report.PASS)
        self.assertEqual(self._kpi(data, "T3")["measured"], "20.0 ms")
        pg_slow = _with_db_writes(res, [400.0] * 10, "postgresql")
        _, data = report.build_report(None, None, self._write("pg2.json", pg_slow))
        self.assertEqual(self._kpi(data, "T3")["status"], report.FAIL)
        lite = _with_db_writes(res, [400.0] * 10, "sqlite")
        md, data = report.build_report(None, None, self._write("lite.json", lite))
        self.assertEqual(self._kpi(data, "T3")["status"], report.INFO)
        self.assertNotIn("T3 Ticket-State DB Write Latency (P95)", data["gates_failed"])
        self.assertIn("T3 not judged", md)

    def test_router_suite_marks_faq_metrics_not_run(self):
        sent = FakeRunner(sentiment_script(lambda m: N))
        results = run(ri.run_all([golden("S-1", "S", N)], sent, None, MODELS, PRICING, 1, 10))
        s = ri.summarize(results, PRICING, {"suite": "router"})
        _, data = report.build_report(None, None, self._write("system_metrics.json", s))
        faq = next(r for r in data["no_target_metrics"]["latency"] if r["key"] == "faq_agent_s")
        self.assertEqual(faq["status"], report.NOT_MEASURED)
        self.assertEqual(faq["source"], "not run (router suite)")

    def test_findings_list_low_judge_cases(self):
        md, _ = report.build_report(self._write("g.json", _grade()), None, None)
        self.assertIn("grounding_key_fact_recall=0", md)
        self.assertIn("missed the baggage fact", md)

    def test_hallucination_metric_version_is_normalized(self):
        g = _grade()
        for m in g["summary_metrics"]:
            if m["metric_name"] == "hallucination_v1":
                m["metric_name"], m["mean_score"] = "hallucination_v2", 0.5   # newer eval SDK
        _, data = report.build_report(self._write("g.json", g), None, None)
        row = next(r for r in data["no_target_metrics"]["llm_judge"] if r["metric"] == "hallucination")
        self.assertEqual(row["measured"], "50.0%")
        self.assertEqual(row["status"], report.INFO)


# ---------------------------------------------------------------------------
# Grading traces from the same run (one inference pass feeds every metric)
# ---------------------------------------------------------------------------
FAQ_INFO = {"agent_id": "faq_assistant_agent", "agent_type": "LlmAgent", "description": "",
            "instruction": "be helpful", "tools": [], "sub_agents": []}


def in_scope(case):
    case["search_ready"] = True
    case["key_facts"] = ["Do X."]
    return case


def _trace_inputs():
    """S-1 answered; S-2 in scope but routed urgent; M-1 multi-turn answered; U-1 and V-1 out of scope."""
    cases = [in_scope(golden("S-1", "S", N, text="neutral q")),
             in_scope(golden("S-2", "S", N, text="angry!!")),
             in_scope(golden("M-1", "M", N, turns=["help", "Spotify: why?"])),
             golden("U-1", "U", U, text="FIX IT!!"),
             dict(golden("V-1", "V", N, text="hi"), search_ready=False)]
    sent = FakeRunner(sentiment_script(lambda m: U if "!!" in m else N))
    results = run(ri.run_all(cases, sent, FakeRunner(faq_script), MODELS, PRICING, 2, 10))
    return cases, results


class TestGradingTraces(unittest.TestCase):

    def test_turn_records_events_for_the_judge(self):
        t = run(ri.run_agent_turn(FakeRunner(faq_script), "m", "s", "q", 10))
        kinds = [next(iter(e["content"]["parts"][0])) for e in t["events"]]
        self.assertEqual(kinds, ["function_call", "function_response", "text"])
        self.assertEqual(t["events"][1]["content"]["parts"][0]["function_response"]["response"]["results"],
                         [{"doc_id": "twcs_1"}])

    def test_only_answered_in_scope_tickets_are_judged(self):
        cases, results = _trace_inputs()
        traces, manifest = ri.build_traces(cases, results, FAQ_INFO)
        ids = [c["eval_case_id"] for c in traces["eval_cases"]]
        self.assertEqual(ids, ["S-1", "M-1"])                        # U-1, V-1 out of scope
        self.assertEqual((manifest["in_scope"], manifest["judged"]), (3, 2))
        self.assertIn("routed to human queue", manifest["skipped"]["S-2"])

    def test_trace_holds_this_runs_answer_and_conversation(self):
        cases, results = _trace_inputs()
        traces, _ = ri.build_traces(cases, results, FAQ_INFO)
        m1 = next(c for c in traces["eval_cases"] if c["eval_case_id"] == "M-1")
        self.assertIn("Do X.", m1["responses"][0]["response"]["parts"][0]["text"])
        turns = m1["agent_data"]["turns"]
        self.assertEqual(len(turns), 1)                              # single-turn metrics need 1 turn
        events = turns[0]["events"]
        users = [e["content"]["parts"][0]["text"] for e in events if e["author"] == "user"]
        self.assertEqual(users, ["help", "Spotify: why?"])          # whole conversation, in order
        self.assertEqual(events[0]["author"], "user")
        self.assertEqual(sum(1 for e in events if e["author"] == "faq_assistant_agent"), 6)  # 3 per turn
        self.assertEqual(m1["agent_data"]["agents"]["faq_assistant_agent"]["instruction"], "be helpful")
        self.assertEqual(m1["key_facts"], ["Do X."])                 # golden fields kept for the judges
        self.assertEqual(m1["prompt"]["parts"][0]["text"], "Spotify: why?")

    def test_faq_error_is_skipped_with_reason(self):
        case = in_scope(golden("S-9", "S", N))
        results = run(ri.run_all([case], FakeRunner(sentiment_script(lambda m: N)),
                                 FakeRunner(None, raise_exc=RuntimeError("429")), MODELS, PRICING, 1, 10))
        traces, manifest = ri.build_traces([case], results, FAQ_INFO)
        self.assertEqual(traces["eval_cases"], [])
        self.assertEqual(manifest["skipped"]["S-9"], "FAQ agent error (rate_limited)")


# ---------------------------------------------------------------------------
# --run-dir report and CI outputs (PR comment, annotations)
# ---------------------------------------------------------------------------
class TestRunDirReport(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        cases, results = _trace_inputs()
        traces, manifest = ri.build_traces(cases, results, FAQ_INFO)
        summary = ri.summarize(results, PRICING, {"suite": "full", "models": MODELS, "git_revision": "abc1234"})
        for name, data in (("system_metrics.json", summary), ("traces.json", traces),
                           ("traces_manifest.json", manifest)):
            with open(os.path.join(self.dir, name), "w") as f:
                json.dump(data, f)

    def tearDown(self):
        self.tmp.cleanup()

    def _main(self, *extra):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report.main(["--run-dir", self.dir, "--out-dir", self.dir, *extra])
        return code, out.getvalue()

    def _data(self):
        with open(os.path.join(self.dir, "kpi_report.json")) as f:
            return json.load(f)

    def test_without_grade_in_run_dir_judges_are_not_measured(self):
        # Even if other grade files exist elsewhere in artifacts/, --run-dir never borrows them.
        self._main()
        data = self._data()
        judge = next(k for k in data["quality_kpis"] if k["id"] == "T4")
        self.assertEqual(judge["status"], report.NOT_MEASURED)
        self.assertIsNone(data["sources"]["grade"])
        self.assertEqual(data["n_cases"], 5)
        self.assertEqual(data["judge_coverage"], {"in_scope": 3, "judged": 2})

    def test_grade_file_in_run_dir_is_used(self):
        os.makedirs(os.path.join(self.dir, "grade"))
        with open(os.path.join(self.dir, "grade", "results_20260101_000000.json"), "w") as f:
            json.dump(_grade(0.9), f)
        self._main()
        judge = next(k for k in self._data()["quality_kpis"] if k["id"] == "T4")
        self.assertEqual((judge["measured"], judge["status"]), ("90.0%", report.PASS))

    def test_pr_comment(self):
        path = os.path.join(self.dir, "pr_comment.md")
        self._main("--pr-comment", path, "--run-url", "https://example/runs/1")
        with open(path) as f:
            body = f.read()
        self.assertTrue(body.startswith(report.PR_COMMENT_MARKER))
        self.assertIn("`abc1234`", body)
        self.assertIn("**5** tickets", body)
        self.assertIn("LLM judge on 2 of 3", body)
        self.assertIn("Report-only", body)
        self.assertIn("| T1 | Routing Accuracy |", body)
        self.assertIn("No TDD / Scoping target", body)
        self.assertIn("| Est. Cost per 1K Tickets |", body)
        self.assertIn("https://example/runs/1", body)
        self.assertIn("Judge coverage", body)

    def test_annotations_and_report_only_exit_code(self):
        code, out = self._main("--annotations")
        self.assertEqual(code, 0)                       # report-only: failing KPIs don't fail CI
        warnings = [l for l in out.splitlines() if l.startswith("::warning")]
        self.assertTrue(any("T1 Routing Accuracy" in w for w in warnings), out)
        self.assertTrue(all("%25" in w or "%" not in w.split("::", 2)[2] for w in warnings))
        code, _ = self._main("--enforce-gates")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
