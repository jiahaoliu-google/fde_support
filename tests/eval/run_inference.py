#!/usr/bin/env python3
"""Pre-deployment agent eval: runs the golden set through the agents in-process.

Measures, from the actual run (nothing hard-coded):
  * routing: sentiment_router_agent label vs. golden expected_sentiment (all cases)
  * latency: per-turn agent latency (sentiment + FAQ when routed to FAQ), search tool time
  * errors:  agent call exceptions / timeouts / 429s, and tool-level search errors
  * tokens + cost: Gemini usage_metadata x rates in tests/eval/pricing.yaml, plus search fees

The agents run locally with ADK Runner + InMemorySessionService (the same agent objects
that get deployed), against real Vertex AI Gemini and the real Discovery Engine datastore.
Latency therefore excludes Agent Runtime / Cloud Run / network hops and the backend's
Model Armor redaction + Cloud SQL writes: it's the agent cost you can optimize before deploy.

Usage (needs `gcloud auth application-default login`):
  python tests/eval/run_inference.py                       # full: router + FAQ, all 100 cases
  python tests/eval/run_inference.py --suite router        # router only (cheap, ~100 flash calls)
  python tests/eval/run_inference.py --limit 10 --concurrency 2
  python tests/eval/run_inference.py --enforce-gates       # exit 1 if a KPI gate fails

Writes artifacts/eval/<UTC timestamp>/:
  raw_results.jsonl      every case and turn, with timings, tokens, tool calls and events
  system_metrics.json    run-level KPIs
  traces.json            (full suite) the FAQ answers of this run for the neutral, search-ready
                         tickets, in agents-cli grading format, for `agents-cli eval grade`
  traces_manifest.json   which in-scope tickets were judged, and why the others weren't
scripts/generate_kpi_report.py --run-dir <dir> builds the report from that one directory.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO_ROOT)

import kpi_metrics as km  # noqa: E402

DEFAULT_GOLDEN = os.path.join(HERE, "golden", "golden_set_v1.json")
DEFAULT_OUT_ROOT = os.path.join(REPO_ROOT, "artifacts", "eval")
SEARCH_TOOL = "search_support_records"
# Same prompt framing the deployed sentiment agent receives from the backend: the raw message.
EVAL_USER_ID = "eval_harness"


# ---------------------------------------------------------------------------
# Golden set helpers
# ---------------------------------------------------------------------------
def user_turns(case: dict) -> list[str]:
    """The scripted customer messages of a case, in order (1 for single-turn cases)."""
    turns = (case.get("agent_data") or {}).get("turns")
    if turns:
        ordered = sorted(turns, key=lambda t: t.get("turn_index", 0))
        return [_text(t.get("prompt")) for t in ordered]
    return [_text(case.get("prompt"))]


def _text(content: Optional[dict]) -> str:
    parts = (content or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def file_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def git_revision() -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=5)
        sha = out.stdout.strip()
        if not sha:
            return None
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------
def _strip_fences(text: str) -> str:
    t = text.strip()
    if "```json" in t:
        t = t.split("```json")[-1].split("```")[0].strip()
    elif t.startswith("```"):
        t = t.strip("`").strip()
    return t


def parse_sentiment_label(text: str) -> str:
    """The predicted label, or INVALID (the backend escalates those as well)."""
    try:
        data = json.loads(_strip_fences(text))
    except (ValueError, TypeError):
        return km.INVALID
    label = data.get("sentiment") if isinstance(data, dict) else None
    return label if label in km.SENTIMENT_LABELS else km.INVALID


def parse_faq_output(text: str) -> Optional[dict]:
    try:
        data = json.loads(_strip_fences(text))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("answer"), str) else None


def classify_exception(exc: BaseException) -> str:
    """Error bucket for M6: timeout / rate_limited / server_error / client_error / other."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    msg = f"{type(exc).__name__} {exc}"
    if code == 429 or "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return "rate_limited"
    if isinstance(code, int) and code >= 500:
        return "server_error"
    if isinstance(code, int) and 400 <= code < 500:
        return "client_error"
    if any(s in msg for s in ("503", "500", "UNAVAILABLE", "INTERNAL", "DEADLINE")):
        return "server_error"
    return "other"


# ---------------------------------------------------------------------------
# One agent turn: timing, tokens, tool calls
# ---------------------------------------------------------------------------
async def run_agent_turn(runner: Any, model: str, session_id: str, message: str,
                         timeout_s: float, clock: Callable[[], float] = time.perf_counter) -> dict:
    """Runs one user message through an ADK runner and measures it.

    `runner` only needs ADK's `run_async(user_id=, session_id=, new_message=)` async
    generator, so tests can pass a fake. Search latency is the time between receiving
    the search function_call event and its function_response event (the tool runs in
    between).
    """
    from google.genai import types  # local: keeps the module importable without ADK

    turn = {
        "model": model, "latency_s": None, "final_text": "", "usage": km.empty_usage(),
        "llm_calls": 0, "tool_calls": [], "search_calls": [], "events": [],
        "error": None, "error_type": None,
    }
    pending_search: dict[str, dict] = {}
    start = clock()

    async def consume():
        msg = types.Content(role="user", parts=[types.Part.from_text(text=message)])
        async for event in runner.run_async(user_id=EVAL_USER_ID, session_id=session_id, new_message=msg):
            now = clock()
            if getattr(event, "partial", False):
                continue
            usage = km.usage_from_metadata(getattr(event, "usage_metadata", None))
            if usage:
                km.add_usage(turn["usage"], usage)
                turn["llm_calls"] += 1
            content = getattr(event, "content", None)
            event_parts = []
            for part in (getattr(content, "parts", None) or []):
                fc = getattr(part, "function_call", None)
                fr = getattr(part, "function_response", None)
                if fc:
                    event_parts.append({"function_call": {"id": fc.id, "name": fc.name, "args": dict(fc.args or {})}})
                    turn["tool_calls"].append(fc.name)
                    if fc.name == SEARCH_TOOL:
                        rec = {"query": (fc.args or {}).get("query"), "latency_s": None, "status": None,
                               "error_type": None, "doc_ids": []}
                        turn["search_calls"].append(rec)
                        pending_search[fc.id or f"anon-{len(turn['search_calls'])}"] = (rec, now)
                elif fr:
                    event_parts.append({"function_response": {"id": fr.id, "name": fr.name,
                                                               "response": fr.response or {}}})
                    if fr.name != SEARCH_TOOL:
                        continue
                    key = fr.id if fr.id in pending_search else next(iter(pending_search), None)
                    if key is not None:
                        rec, started = pending_search.pop(key)
                        rec["latency_s"] = now - started
                        resp = fr.response or {}
                        rec["status"] = resp.get("status")
                        rec["error_type"] = resp.get("error_type")
                        rec["doc_ids"] = [r.get("doc_id") for r in resp.get("results", []) if isinstance(r, dict)]
                elif getattr(part, "text", None) and not getattr(part, "thought", False):
                    event_parts.append({"text": part.text})
                    turn["final_text"] += part.text
            if event_parts:
                role = getattr(content, "role", None) or (
                    "user" if any("function_response" in p for p in event_parts) else "model")
                turn["events"].append({"author": getattr(event, "author", None) or "agent",
                                       "content": {"role": role, "parts": event_parts}})

    try:
        await asyncio.wait_for(consume(), timeout=timeout_s)
    except Exception as e:  # noqa: BLE001 - every failure is a measured outcome
        turn["error"] = f"{type(e).__name__}: {e}"[:500]
        turn["error_type"] = classify_exception(e)
    turn["latency_s"] = clock() - start
    return turn


def turn_cost(turn: Optional[dict], pricing: dict) -> tuple[Optional[float], Optional[str]]:
    """(USD, None) or (None, reason) for one agent turn: model tokens + search queries."""
    if not turn:
        return 0.0, None
    model_cost = km.model_call_cost(turn["model"], turn["usage"], pricing)
    if model_cost is None:
        return None, f"no price for model {turn['model']} in pricing.yaml"
    n_search = len(turn["search_calls"])
    s_cost = km.search_cost(n_search, pricing) if n_search else 0.0
    if s_cost is None:
        return None, "no search price in pricing.yaml"
    return model_cost + s_cost, None


async def create_session(runner: Any, session_id: str) -> None:
    """Creates the session up front. Works across ADK versions: `create_session` is sync in
    0.x and async in >=1.0, and `Runner(auto_create_session=...)` doesn't exist everywhere.
    Runners without a session service (test fakes) are skipped."""
    svc = getattr(runner, "session_service", None)
    if svc is None:
        return
    result = svc.create_session(app_name=runner.app_name, user_id=EVAL_USER_ID, session_id=session_id)
    if inspect.isawaitable(result):
        await result


# ---------------------------------------------------------------------------
# One golden case: what the production pipeline would do with each message
# ---------------------------------------------------------------------------
async def run_case(case: dict, sentiment_runner: Any, faq_runner: Optional[Any], models: dict,
                   pricing: dict, timeout_s: float) -> dict:
    """Mirrors /api/chat routing per message: classify, then FAQ only if NEUTRAL.

    An URGENT, INVALID or failed classification escalates the ticket (fail closed), so
    later scripted turns aren't sent. The case-level prediction is the label that
    decided the route.
    """
    faq_session = f"eval-{case['eval_case_id']}-{uuid.uuid4().hex[:8]}"
    if faq_runner is not None:
        await create_session(faq_runner, faq_session)
    turns_out = []
    predicted = km.NEUTRAL
    for i, message in enumerate(user_turns(case), start=1):
        # Fresh sentiment session per message, like the backend (s-<uuid>).
        sentiment_session = f"s-{uuid.uuid4().hex}"
        await create_session(sentiment_runner, sentiment_session)
        s_turn = await run_agent_turn(sentiment_runner, models["sentiment"], sentiment_session,
                                      message, timeout_s)
        label = km.ERROR if s_turn["error"] else parse_sentiment_label(s_turn["final_text"])
        t = {"turn": i, "message": message, "sentiment_label": label, "sentiment": s_turn, "faq": None,
             "route": "FAQ_AGENT" if label == km.NEUTRAL else "HUMAN_QUEUE"}

        if label == km.NEUTRAL and faq_runner is not None:
            f_turn = await run_agent_turn(faq_runner, models["faq"], faq_session, message, timeout_s)
            parsed = None if f_turn["error"] else parse_faq_output(f_turn["final_text"])
            f_turn["output_valid"] = parsed is not None
            f_turn["answer"] = parsed.get("answer") if parsed else None
            f_turn["escalate"] = parsed.get("escalate") if parsed else None
            f_turn["grounded"] = parsed.get("grounded") if parsed else None
            t["faq"] = f_turn

        parts = [x for x in (t["sentiment"], t["faq"]) if x]
        t["latency_s"] = sum(x["latency_s"] or 0.0 for x in parts)
        costs = [turn_cost(x, pricing) for x in parts]
        reasons = [r for c, r in costs if c is None]
        t["cost_usd"] = None if reasons else sum(c for c, _ in costs)
        t["cost_unpriced_reason"] = reasons[0] if reasons else None
        turns_out.append(t)

        if label != km.NEUTRAL:
            predicted = label
            break

    errors = [x for t in turns_out for x in (t["sentiment"], t["faq"]) if x and x["error"]]
    unpriced = [t["cost_unpriced_reason"] for t in turns_out if t["cost_unpriced_reason"]]
    return {
        "eval_case_id": case["eval_case_id"],
        "segment": case.get("segment"),
        "expected_sentiment": case.get("expected_sentiment"),
        "expected_route": case.get("expected_route"),
        "predicted_sentiment": predicted,
        "predicted_route": "FAQ_AGENT" if predicted == km.NEUTRAL else "HUMAN_QUEUE",
        "turns": turns_out,
        "errored": bool(errors),
        "error_types": sorted({e["error_type"] for e in errors}),
        "cost_usd": None if unpriced else sum(t["cost_usd"] for t in turns_out),
        "cost_unpriced_reason": unpriced[0] if unpriced else None,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def summarize(results: list[dict], pricing: dict, run_info: dict) -> dict:
    """Run-level KPIs from per-case results (pure; unit-tested)."""
    routing = km.classification_report((r["expected_sentiment"], r["predicted_sentiment"]) for r in results)
    urgent = routing["per_class"][km.URGENT]

    turns = [t for r in results for t in r["turns"]]
    agent_turns = [x for t in turns for x in (t["sentiment"], t["faq"]) if x]
    faq_turns = [t["faq"] for t in turns if t["faq"]]
    searches = [s for f in faq_turns for s in f["search_calls"]]

    e2e = km.latency_summary(t["latency_s"] for t in turns if not _turn_errored(t))
    sentiment_lat = km.latency_summary(t["sentiment"]["latency_s"] for t in turns if not t["sentiment"]["error"])
    faq_lat = km.latency_summary(f["latency_s"] for f in faq_turns if not f["error"])
    search_lat = km.latency_summary(s["latency_s"] for s in searches if s["latency_s"] is not None)

    error_types: dict[str, int] = {}
    for x in agent_turns:
        if x["error"]:
            error_types[x["error_type"]] = error_types.get(x["error_type"], 0) + 1
    search_errors: dict[str, int] = {}
    for s in searches:
        if s["status"] == "error":
            k = s["error_type"] or "unknown"
            search_errors[k] = search_errors.get(k, 0) + 1
    n_err_calls = sum(error_types.values())
    errors = {
        "agent_calls": len(agent_turns),
        "agent_call_errors": n_err_calls,
        "error_rate": km._safe_div(n_err_calls, len(agent_turns)),
        "by_type": error_types,
        "cases_with_errors": sum(1 for r in results if r["errored"]),
        "search_calls": len(searches),
        "search_tool_errors": search_errors,
        "invalid_sentiment_outputs": routing["invalid_predictions"],
        "invalid_faq_outputs": sum(1 for f in faq_turns if not f["error"] and not f.get("output_valid")),
    }

    tokens = {"sentiment": km.empty_usage(), "faq": km.empty_usage()}
    for t in turns:
        km.add_usage(tokens["sentiment"], t["sentiment"]["usage"])
        if t["faq"]:
            km.add_usage(tokens["faq"], t["faq"]["usage"])

    priced = [r for r in results if r["cost_usd"] is not None]
    unpriced = sorted({r["cost_unpriced_reason"] for r in results if r["cost_unpriced_reason"]})
    by_segment: dict[str, dict] = {}
    for r in priced:
        seg = by_segment.setdefault(r["segment"] or "?", {"n": 0, "total_usd": 0.0})
        seg["n"] += 1
        seg["total_usd"] += r["cost_usd"]
    for seg in by_segment.values():
        seg["per_ticket_usd"] = seg["total_usd"] / seg["n"]
    total_cost = sum(r["cost_usd"] for r in priced) if priced and not unpriced else None
    cost = {
        "total_usd": total_cost,
        "per_ticket_usd": total_cost / len(results) if total_cost is not None and results else None,
        "per_1k_tickets_usd": total_cost / len(results) * 1000 if total_cost is not None and results else None,
        "by_segment": by_segment,
        "unpriced_reasons": unpriced,
        "search_edition": (pricing.get("search") or {}).get("edition"),
        "note": "Golden-set ticket mix; urgent tickets only pay for the router. "
                "Excludes LLM-judge cost and Cloud Run / Cloud SQL infrastructure.",
    }

    targets = km.KPI_TARGETS
    gates = {
        "routing_accuracy": km.gate(routing["accuracy"], targets["routing_accuracy"]),
        "urgent_f1": km.gate(urgent["f1"], targets["urgent_f1"]),
        "e2e_latency_p95_s": km.gate(e2e and e2e["p95"], targets["e2e_latency_p95_s"], higher_is_better=False),
        "search_latency_p95_s": km.gate(search_lat and search_lat["p95"], targets["search_latency_p95_s"],
                                        higher_is_better=False),
        "error_rate": km.gate(errors["error_rate"], targets["error_rate"], higher_is_better=False),
    }

    misrouted = [
        {"eval_case_id": r["eval_case_id"], "segment": r["segment"],
         "expected": r["expected_sentiment"], "predicted": r["predicted_sentiment"],
         "error_types": r["error_types"]}
        for r in results if r["expected_sentiment"] != r["predicted_sentiment"]
    ]
    slowest = sorted(
        ({"eval_case_id": r["eval_case_id"], "turn": t["turn"], "latency_s": t["latency_s"],
          "route": t["route"], "search_calls": len(t["faq"]["search_calls"]) if t["faq"] else 0}
         for r in results for t in r["turns"]),
        key=lambda x: x["latency_s"] or 0, reverse=True)[:5]

    return {
        "schema_version": 1,
        "run": run_info,
        "n_cases": len(results),
        "n_turns": len(turns),
        "segments": _count(r["segment"] for r in results),
        "routing": routing,
        "latency_s": {"e2e_turn": e2e, "sentiment": sentiment_lat, "faq": faq_lat, "search_tool": search_lat},
        "errors": errors,
        "tokens": tokens,
        "cost": cost,
        "targets": targets,
        "gates": gates,
        "misrouted": misrouted,
        "slowest_turns": slowest,
    }


def _turn_errored(t: dict) -> bool:
    return bool(t["sentiment"]["error"] or (t["faq"] and t["faq"]["error"]))


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Grading traces: the same answers, in agents-cli EvaluationDataset format
# ---------------------------------------------------------------------------
def agent_descriptor(agent: Any) -> dict:
    """The `agent_data.agents` entry agents-cli writes (id, instruction, tool declarations)."""
    info = {"agent_id": getattr(agent, "name", "agent"), "agent_type": type(agent).__name__,
            "description": getattr(agent, "description", "") or "",
            "instruction": agent.instruction if isinstance(getattr(agent, "instruction", None), str) else "",
            "tools": [], "sub_agents": []}
    try:
        from google.adk.tools.function_tool import FunctionTool
        for t in getattr(agent, "tools", None) or []:
            tool = t if hasattr(t, "_get_declaration") else FunctionTool(t)
            decl = tool._get_declaration()
            if decl is not None:
                info["tools"].append({"function_declarations": [decl.model_dump(mode="json", exclude_none=True)]})
    except Exception:  # noqa: BLE001 - declarations are context for the judge, not required
        pass
    return info


def _not_judgeable(case: dict, result: Optional[dict]) -> Optional[str]:
    """Why an in-scope ticket has no answer to grade, or None if it has one."""
    if result is None:
        return "not run"
    if result["predicted_sentiment"] != km.NEUTRAL or len(result["turns"]) < len(user_turns(case)):
        return f"routed to human queue ({result['predicted_sentiment']})"
    last = result["turns"][-1]["faq"]
    if last is None:
        return "FAQ agent not run"
    if last["error"]:
        return f"FAQ agent error ({last['error_type']})"
    return None


def build_traces(cases: list[dict], results: list[dict], faq_agent_info: dict) -> tuple[dict, dict]:
    """(EvaluationDataset, manifest) for the neutral, search-ready tickets of this run.

    Scope comes from scripts/build_eval_datasets.in_rubric_scope (the single source of
    truth). Each case keeps its golden fields (key_facts, rubric_items, reference...) for
    the judges; `responses` is the final FAQ answer and `agent_data.turns` holds every
    user message, tool call, tool response and answer of the conversation.
    """
    from scripts.build_eval_datasets import in_rubric_scope, to_inference_case

    by_id = {r["eval_case_id"]: r for r in results}
    agent_id = faq_agent_info["agent_id"]
    eval_cases, skipped, in_scope = [], {}, 0
    for case in cases:
        if not in_rubric_scope(case):
            continue
        in_scope += 1
        result = by_id.get(case["eval_case_id"])
        reason = _not_judgeable(case, result)
        if reason:
            skipped[case["eval_case_id"]] = reason
            continue
        out = to_inference_case(case)
        turns = []
        for t in result["turns"]:
            user = {"author": "user", "content": {"role": "user", "parts": [{"text": t["message"]}]}}
            events = [user] + [dict(e, author=agent_id) for e in t["faq"]["events"]]
            turns.append({"turn_index": t["turn"] - 1, "turn_id": f"turn_{t['turn'] - 1}", "events": events})
        final = result["turns"][-1]["faq"]["final_text"]
        out["responses"] = [{"response": {"role": "model", "parts": [{"text": final}]}}]
        out["agent_data"] = {"agents": {agent_id: faq_agent_info}, "turns": turns}
        eval_cases.append(out)
    manifest = {"in_scope": in_scope, "judged": len(eval_cases), "skipped": skipped,
                "judged_case_ids": [c["eval_case_id"] for c in eval_cases]}
    return {"eval_cases": eval_cases}, manifest


# ---------------------------------------------------------------------------
# Runner wiring (real agents) and CLI
# ---------------------------------------------------------------------------
def build_runners(suite: str):
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from agents.sentiment_router.agent import sentiment_router_agent

    models = {"sentiment": str(sentiment_router_agent.model)}
    # Sessions are created explicitly in run_case (see create_session), not via
    # Runner(auto_create_session=...), which only some ADK versions accept.
    sentiment = Runner(app_name="eval_sentiment", agent=sentiment_router_agent,
                       session_service=InMemorySessionService())
    faq, faq_info = None, None
    if suite == "full":
        from agents.faq_assistant.agent import faq_assistant_agent
        models["faq"] = str(faq_assistant_agent.model)
        faq = Runner(app_name="eval_faq", agent=faq_assistant_agent,
                     session_service=InMemorySessionService())
        faq_info = agent_descriptor(faq_assistant_agent)
    return sentiment, faq, models, faq_info


async def run_all(cases: list[dict], sentiment_runner, faq_runner, models, pricing,
                  concurrency: int, timeout_s: float) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(case):
        nonlocal done
        async with sem:
            r = await run_case(case, sentiment_runner, faq_runner, models, pricing, timeout_s)
        done += 1
        flag = "ERR" if r["errored"] else ("ok" if r["expected_sentiment"] == r["predicted_sentiment"] else "MISROUTED")
        print(f"[{done:3d}/{len(cases)}] {r['eval_case_id']:6s} {r['predicted_sentiment']:18s} {flag}", flush=True)
        return r

    results = await asyncio.gather(*(one(c) for c in cases))
    order = {c["eval_case_id"]: i for i, c in enumerate(cases)}
    return sorted(results, key=lambda r: order[r["eval_case_id"]])


def print_summary(s: dict) -> None:
    def pct(v): return "n/a" if v is None else f"{v * 100:.1f}%"
    def sec(v): return "n/a" if v is None else f"{v:.2f}s"
    def gate(k): return {True: "PASS", False: "FAIL", None: "NOT MEASURED"}[s["gates"][k]]
    urgent = s["routing"]["per_class"][km.URGENT]
    lat = s["latency_s"]
    print("\n" + "=" * 64)
    print(f" {s['n_cases']} cases / {s['n_turns']} turns  ({s['run']['suite']} suite)")
    print(f" Routing accuracy    {pct(s['routing']['accuracy']):>8}   {gate('routing_accuracy')}")
    f1 = "n/a" if urgent["f1"] is None else f"{urgent['f1']:.3f}"
    print(f" Urgent F1           {f1:>8}   {gate('urgent_f1')}")
    print(f" E2E turn P95        {sec(lat['e2e_turn'] and lat['e2e_turn']['p95']):>8}   {gate('e2e_latency_p95_s')}")
    print(f" Search tool P95     {sec(lat['search_tool'] and lat['search_tool']['p95']):>8}   {gate('search_latency_p95_s')}")
    print(f" Agent error rate    {pct(s['errors']['error_rate']):>8}   {gate('error_rate')}")
    c = s["cost"]["per_1k_tickets_usd"]
    print(f" Cost / 1K tickets   {'n/a' if c is None else f'${c:.2f}':>8}   (report only)")
    print("=" * 64)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--golden", default=DEFAULT_GOLDEN)
    p.add_argument("--suite", choices=("full", "router"), default="full",
                   help="full = sentiment + FAQ (latency/cost of the whole path); router = sentiment only")
    p.add_argument("--segments", default="", help="comma-separated segment filter, e.g. U,S")
    p.add_argument("--limit", type=int, default=0, help="first N cases (after the segment filter)")
    p.add_argument("--concurrency", type=int, default=4,
                   help="cases in flight; keep low so the eval itself doesn't cause 429s")
    p.add_argument("--timeout", type=float, default=60.0, help="per agent call, seconds")
    p.add_argument("--pricing", default=km.DEFAULT_PRICING_PATH)
    p.add_argument("--out-root", default=DEFAULT_OUT_ROOT)
    p.add_argument("--enforce-gates", action="store_true", help="exit 1 if any measured gate fails")
    p.add_argument("--github-output", action="store_true",
                   help="write run_dir=<path> to $GITHUB_OUTPUT for later workflow steps")
    args = p.parse_args(argv)

    with open(args.golden, encoding="utf-8") as f:
        cases = json.load(f)
    if args.segments:
        wanted = {s.strip() for s in args.segments.split(",") if s.strip()}
        cases = [c for c in cases if c.get("segment") in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 2

    pricing = km.load_pricing(args.pricing)
    sentiment_runner, faq_runner, models, faq_info = build_runners(args.suite)

    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    results = asyncio.run(run_all(cases, sentiment_runner, faq_runner, models, pricing,
                                  args.concurrency, args.timeout))
    run_info = {
        "started_at": started.isoformat(),
        "duration_s": round(time.perf_counter() - t0, 2),
        "suite": args.suite,
        "golden_file": os.path.relpath(args.golden, REPO_ROOT),
        "golden_sha256": file_sha256(args.golden),
        "segments_filter": args.segments or None,
        "limit": args.limit or None,
        "concurrency": args.concurrency,
        "timeout_s": args.timeout,
        "models": models,
        "git_revision": git_revision(),
        "target": "in-process (ADK Runner, InMemorySessionService)",
        "pricing_file": os.path.relpath(args.pricing, REPO_ROOT),
    }
    summary = summarize(results, pricing, run_info)

    out_dir = os.path.join(args.out_root, started.strftime("%Y%m%dT%H%M%SZ"))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "raw_results.jsonl"), "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    with open(os.path.join(out_dir, "system_metrics.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    if faq_info is not None:
        traces, manifest = build_traces(cases, results, faq_info)
        with open(os.path.join(out_dir, "traces.json"), "w", encoding="utf-8") as f:
            json.dump(traces, f, indent=2, ensure_ascii=False, default=str)
        with open(os.path.join(out_dir, "traces_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"Grading traces: {manifest['judged']} of {manifest['in_scope']} in-scope tickets "
              f"({len(manifest['skipped'])} without an answer to grade)")

    print_summary(summary)
    print(f"Wrote {os.path.relpath(out_dir, REPO_ROOT)}/")
    if args.github_output and os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"run_dir={out_dir}\n")
    if args.enforce_gates and any(v is False for v in summary["gates"].values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
