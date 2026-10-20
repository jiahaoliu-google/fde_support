#!/usr/bin/env python3
"""
Generate kpi_report.md / kpi_report.json from evaluation artifacts.

The report has two kinds of rows:
  * TDD KPIs (T1, T3-T8): judged PASS / FAIL against the target written in the TDD.
  * Metrics with no TDD / Scoping target (agent + Model Armor latency, extra LLM-judge
    metrics, tokens, cost): reported as measured values only. They never get a made-up
    target and never fail a gate.

Every number is computed from a run artifact. Anything without a source artifact is
shown as NOT MEASURED, never as a default value:
  * T1, T3, T5, latency, tokens, cost:  system_metrics.json (tests/eval/run_inference.py)
  * T6 ROUGE-L, T7 corpus BLEU-4:        traces.json (same run) or artifacts/traces/*.json
  * T4, T8 and other LLM-judge metrics:  grade results JSON (agents-cli eval grade)

Usage:
  python scripts/eval/generate_kpi_report.py --run-dir artifacts/eval/<ts>   # all sections from one run (CI)
  python scripts/eval/generate_kpi_report.py                 # latest artifact of each kind
  python scripts/eval/generate_kpi_report.py --system artifacts/eval/<ts>/system_metrics.json
  python scripts/eval/generate_kpi_report.py --enforce-gates # exit 1 if a measured TDD gate fails
  ... --pr-comment out.md --run-url <url>                    # compact table for a PR comment
  ... --annotations                                          # ::warning:: per TDD KPI below target
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from typing import Optional

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

import yaml  # noqa: E402

from tests.eval.static_rubric_evaluator import TDD_GATES, evaluate_static_rubric, corpus_bleu  # noqa: E402
from tests.eval import kpi_metrics as km  # noqa: E402

PASS, FAIL, NOT_MEASURED, INFO = "PASS", "FAIL", "NOT MEASURED", "INFO"
BADGE = {PASS: "🟢 **PASS**", FAIL: "🔴 **FAIL**", NOT_MEASURED: "⚪ NOT MEASURED", INFO: "ℹ️ INFO"}
SHORT_BADGE = {PASS: "✅", FAIL: "❌", NOT_MEASURED: "⚪", INFO: "ℹ️"}
PR_COMMENT_MARKER = "<!-- fde-support-agent-eval -->"
DEFAULT_EVAL_CONFIG = os.path.join(REPO_ROOT, "tests", "eval", "eval_config_faq_rubric.yaml")
NO_TARGET = "— (no TDD/Scoping target)"

# LLM-judge KPIs with a TDD target. Values come from static_rubric_evaluator.TDD_GATES so
# the report and the evaluator can't drift apart.
TDD_JUDGE_KPIS = [
    # (id, name, metric_name, threshold, TDD section)
    ("T4", "Grounding Recall", "grounding_key_fact_recall", TDD_GATES["grounding_recall"], "North Star"),
    ("T8", "RAG Faithfulness (hallucinations + policy)", "rag_faithfulness", TDD_GATES["faithfulness"],
     "Semantic QA (Nightly)"),
]
# LLM-judge metrics in eval_config_faq_rubric.yaml that have no TDD / Scoping target.
UNTARGETED_JUDGE_METRICS = [
    ("Rubric Scope Guard (neutral, search-ready only)", "rubric_scope_check"),
    ("Case Rubric Adherence", "case_rubric_adherence"),
    ("Hallucination (managed metric)", "hallucination"),
]
RUN_ROUTER_HINT = "run `python tests/eval/run_inference.py`"


# ---------------------------------------------------------------------------
# Artifact discovery
# ---------------------------------------------------------------------------
def latest(pattern: str) -> Optional[str]:
    """Most recently modified file matching the glob (names differ across directories)."""
    files = [f for f in glob.glob(pattern, recursive=True) if os.path.isfile(f)]
    return max(files, key=os.path.getmtime) if files else None


def load_json(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_dir_paths(run_dir: str) -> dict:
    """Artifacts of one run_inference.py run directory (None for any that are missing)."""
    def existing(p):
        return p if os.path.isfile(p) else None
    return {
        "system": existing(os.path.join(run_dir, "system_metrics.json")),
        "trace": existing(os.path.join(run_dir, "traces.json")),
        "manifest": existing(os.path.join(run_dir, "traces_manifest.json")),
        # agents-cli eval grade --output <run_dir>/grade writes results_<ts>.json
        "grade": latest(os.path.join(run_dir, "grade", "**", "results_*.json")),
    }


def judge_models(config_path: str = DEFAULT_EVAL_CONFIG) -> list[str]:
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except OSError:
        return []
    found: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "judge_model" and isinstance(v, str) and v not in found:
                    found.append(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(cfg)
    return found


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------
def lexical_metrics(tdata: Optional[dict]) -> dict:
    """Mean ROUGE-L and corpus BLEU-4 over cases with a historical reference."""
    out = {"rouge_l": None, "bleu4": None, "n": 0, "per_case": {}, "case_ids": []}
    if not tdata:
        return out
    cands, refs, rouges = [], [], []
    for c in tdata.get("eval_cases", []):
        cid = c.get("eval_case_id")
        out["case_ids"].append(cid)
        raw = c.get("responses") or []
        if not raw:
            continue
        text = (raw[0].get("response", {}).get("parts") or [{}])[0].get("text", "")
        try:
            text = json.loads(text).get("answer", text)
        except (ValueError, AttributeError):
            pass
        ev = evaluate_static_rubric(c, text, [], True, True)
        rl = (ev.get("lexical") or {}).get("rouge_l")
        out["per_case"][cid] = rl
        if rl is not None:
            rouges.append(rl)
        if ev.get("_lexical_pair"):
            cand, ref = ev["_lexical_pair"]
            cands.append(cand)
            refs.append(ref)
    out["n"] = len(rouges)
    out["rouge_l"] = sum(rouges) / len(rouges) if rouges else None
    out["bleu4"] = corpus_bleu(cands, refs) if cands else None
    return out


def canonical_metric(name: Optional[str]) -> Optional[str]:
    """The eval SDK versions managed metrics (hallucination_v1 locally, hallucination_v2 in
    a newer SDK). Map every version to one key so a version bump doesn't drop the metric."""
    return re.sub(r"_v\d+$", "", name) if name and name.startswith("hallucination") else name


def judge_metrics(gdata: Optional[dict]) -> dict:
    """{metric_name: {"mean": float|None, "n": int}} from the grade file summary."""
    out = {}
    for m in (gdata or {}).get("summary_metrics", []):
        valid = m.get("num_cases_valid") or 0
        out[canonical_metric(m.get("metric_name"))] = {
            "mean": m.get("mean_score") if valid else None,
            "n": valid,
            "errors": m.get("num_cases_error") or 0,
        }
    return out


def judge_cases(gdata: Optional[dict], case_ids: list[str]) -> list[dict]:
    cases = []
    for case in (gdata or {}).get("eval_case_results", []):
        idx = case.get("eval_case_index")
        metrics = {}
        for cand in case.get("response_candidate_results", []):
            for name, val in (cand.get("metric_results") or {}).items():
                metrics[canonical_metric(name)] = {"score": val.get("score"), "explanation": val.get("explanation") or ""}
        cid = case_ids[idx] if isinstance(idx, int) and idx < len(case_ids) else f"Index {idx}"
        cases.append({"index": idx, "case_id": cid, "metrics": metrics})
    return cases


def status(passed: Optional[bool]) -> str:
    return NOT_MEASURED if passed is None else (PASS if passed else FAIL)


def fmt_pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def fmt_num(v: Optional[float], digits: int = 4) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def fmt_s(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.2f} s"


def fmt_ms(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.1f} ms"


def fmt_usd(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:.2f}"


_LEGACY_LATENCY = {"sentiment_agent_s": "sentiment", "faq_agent_s": "faq"}  # schema 1 keys


def _latency(system: Optional[dict], key: str) -> Optional[dict]:
    system = system or {}
    if "latency" in system:
        return system["latency"].get(key)
    return (system.get("latency_s") or {}).get(_LEGACY_LATENCY.get(key, ""))


# ---------------------------------------------------------------------------
# TDD KPI rows (T1, T3-T8), each judged against its TDD target
# ---------------------------------------------------------------------------
def t3_row(system: Optional[dict]) -> dict:
    """T3: p95 ticket-state write latency <= 100 ms. Judged only on Cloud SQL (PostgreSQL)."""
    target = km.KPI_TARGETS["db_write_p95_ms"]
    row = {"id": "T3", "name": "Ticket-State DB Write Latency (P95)", "target": f"≤ {target:.0f} ms",
           "tdd": "North Star", "value": None, "measured": "—", "source": RUN_ROUTER_HINT,
           "status": NOT_MEASURED}
    d = _latency(system, "db_write_ms")
    if not d:
        if system:
            row["source"] = "no DB writes recorded"
        return row
    db = system.get("db") or {}
    errors = (system.get("errors") or {}).get("db_write_errors") or 0
    row["value"] = d["p95"]
    row["measured"] = fmt_ms(d["p95"])
    src = f"{db.get('dialect', '?')} eval DB, n={d['n']} writes"
    if errors:
        src += f", {errors} failed"
    if db.get("t3_gated"):
        row["status"] = status(system["gates"].get("db_write_p95_ms"))
    else:
        # TDD target is for Cloud SQL; another engine's latency says nothing about it.
        row["status"] = INFO
        src += "; not Cloud SQL, so not judged (use --db-url)"
    row["source"] = src
    return row


def quality_kpis(system: Optional[dict], lexical: dict, judge: dict) -> list[dict]:
    rows = []
    routing = (system or {}).get("routing")
    sys_src = f"router eval, n={routing['n']}" if routing else RUN_ROUTER_HINT
    acc = routing["accuracy"] if routing else None
    f1 = routing["per_class"][km.URGENT]["f1"] if routing else None
    rows.append({"id": "T1", "name": "Routing Accuracy", "target": f"≥ {km.KPI_TARGETS['routing_accuracy']:.0%}",
                 "value": acc, "measured": fmt_pct(acc), "tdd": "North Star", "source": sys_src,
                 "status": status(km.gate(acc, km.KPI_TARGETS["routing_accuracy"]))})
    rows.append(t3_row(system))

    def judge_row(kid, name, metric, threshold, tdd):
        m = judge.get(metric) or {"mean": None, "n": 0, "errors": 0}
        src = f"LLM judge, n={m['n']}" if m["n"] else "no grade file"
        if m.get("errors"):
            src += f", {m['errors']} judge errors"
        return {"id": kid, "name": name, "target": f"≥ {threshold:.0%}", "value": m["mean"],
                "measured": fmt_pct(m["mean"]), "tdd": tdd, "source": src,
                "status": status(km.gate(m["mean"], threshold))}

    t4, t8 = (judge_row(*k) for k in TDD_JUDGE_KPIS)
    rows.append(t4)
    rows.append({"id": "T5", "name": "Urgent/Frustrated F1", "target": f"≥ {km.KPI_TARGETS['urgent_f1']:.2f}",
                 "value": f1, "measured": fmt_num(f1, 3), "tdd": "CI gate (PR)", "source": sys_src,
                 "status": status(km.gate(f1, km.KPI_TARGETS["urgent_f1"]))})

    lex_src = f"trace file, n={lexical['n']}" if lexical["n"] else "no trace file"
    rows.append({"id": "T6", "name": "ROUGE-L vs Historical Reply", "target": f"≥ {TDD_GATES['rouge_l']:.2f}",
                 "value": lexical["rouge_l"], "measured": fmt_num(lexical["rouge_l"]), "tdd": "CI gate (PR)",
                 "source": lex_src, "status": status(km.gate(lexical["rouge_l"], TDD_GATES["rouge_l"]))})
    rows.append({"id": "T7", "name": "BLEU vs Historical Reply (corpus BLEU-4)", "target": f"≥ {TDD_GATES['bleu']:.2f}",
                 "value": lexical["bleu4"], "measured": fmt_num(lexical["bleu4"]), "tdd": "CI gate (PR)",
                 "source": lex_src, "status": status(km.gate(lexical["bleu4"], TDD_GATES["bleu"]))})
    rows.append(t8)
    return rows


# ---------------------------------------------------------------------------
# Rows with no TDD / Scoping target: measured and shown, never judged
# ---------------------------------------------------------------------------
def latency_rows(system: Optional[dict]) -> list[dict]:
    """Per-component latency. P50 / P90 / P95 / max, no target."""
    router_only = bool(system) and system["run"].get("suite") == "router"
    armor = (system or {}).get("model_armor") or {}
    specs = [
        ("Sentiment Agent (per call)", "sentiment_agent_s", fmt_s, "agent call, in-process ADK Runner"),
        ("FAQ Agent (per call, incl. search)", "faq_agent_s", fmt_s, "agent call, in-process ADK Runner"),
        ("Model Armor Redaction (per message)", "model_armor_redaction_ms", fmt_ms,
         f"{armor.get('path') or '?'} path, {armor.get('messages_changed', 0)}/{armor.get('messages', 0)} "
         "messages changed"),
    ]
    rows = []
    for name, key, fmt, src in specs:
        d = _latency(system, key)
        row = {"name": name, "key": key, "target": NO_TARGET, "source": src,
               **{k: fmt(d[k]) if d else "—" for k in ("p50", "p90", "p95", "max")},
               "n": d["n"] if d else 0, "measured": fmt(d["p95"]) if d else "—",
               "status": INFO if d else NOT_MEASURED}
        if not system:
            row["source"] = RUN_ROUTER_HINT
        elif key == "faq_agent_s" and router_only:
            row["source"] = "not run (router suite)"
        elif key == "model_armor_redaction_ms" and armor.get("path") in ("regex", "mixed"):
            by_path = armor.get("by_path") or {}
            row["source"] += (f"; ⚠️ Model Armor used for {by_path.get('model_armor', 0)}, Cloud DLP for "
                              f"{by_path.get('dlp', 0)} of {armor.get('messages', 0)} messages, the rest "
                              "regex only (services disabled, unavailable or failed)")
        rows.append(row)
    return rows


def untargeted_judge_rows(judge: dict) -> list[dict]:
    rows = []
    for name, metric in UNTARGETED_JUDGE_METRICS:
        m = judge.get(metric) or {"mean": None, "n": 0, "errors": 0}
        src = f"LLM judge, n={m['n']}" if m["n"] else "no grade file"
        if m.get("errors"):
            src += f", {m['errors']} judge errors"
        rows.append({"name": name, "metric": metric, "target": NO_TARGET, "measured": fmt_pct(m["mean"]),
                     "value": m["mean"], "source": src, "status": INFO if m["mean"] is not None else NOT_MEASURED})
    return rows


def cost_rows(system: Optional[dict]) -> list[dict]:
    if not system:
        return [{"name": "Est. Cost per 1K Tickets", "target": NO_TARGET, "measured": "—",
                 "source": RUN_ROUTER_HINT, "status": NOT_MEASURED}]
    cost = system["cost"]
    router_only = system["run"].get("suite") == "router"
    v = cost.get("per_1k_tickets_usd")
    return [{"name": "Est. Cost per 1K Tickets" + (" (router only)" if router_only else ""),
             "target": NO_TARGET, "measured": fmt_usd(v), "value": v,
             "source": "; ".join(cost.get("unpriced_reasons") or [])
                       or f"pricing.yaml, search edition={cost.get('search_edition')}",
             "status": INFO if v is not None else NOT_MEASURED}]


# ---------------------------------------------------------------------------
# Findings (generated from the data, never written by hand)
# ---------------------------------------------------------------------------
def findings(system: Optional[dict], lexical: dict, judge: dict, cases: list[dict],
             kpis: list[dict], manifest: Optional[dict] = None) -> list[str]:
    out = []
    failing = [f"{k['id']} {k['name']} ({k['measured']} vs {k['target']})" for k in kpis if k["status"] == FAIL]
    missing = [f"{k['id']} {k['name']}" for k in kpis if k["status"] == NOT_MEASURED]
    out.append(f"**Failing TDD gates:** {'; '.join(failing)}." if failing
               else "**Failing TDD gates:** none of the measured TDD gates fail.")
    if missing:
        out.append(f"**Not measured:** {', '.join(missing)}.")
    t3 = next((k for k in kpis if k["id"] == "T3"), None)
    if t3 and t3["status"] == INFO:
        out.append(f"**T3 not judged:** {t3['measured']} was measured on {(system.get('db') or {}).get('dialect')}, "
                   "not Cloud SQL. Rerun with `--db-url` pointing at Cloud SQL (cloud-sql-proxy) to judge it.")

    sizes = []
    if system:
        sizes.append(f"routing n={system['routing']['n']} ({', '.join(f'{k}={v}' for k, v in sorted(system['segments'].items()))})")
    if lexical["n"]:
        sizes.append(f"lexical n={lexical['n']}")
    judged = max((m["n"] for m in judge.values()), default=0)
    if judged:
        sizes.append(f"LLM judge n={judged}")
    if sizes:
        note = " Small samples move a lot per case." if any(s for s in (lexical["n"], judged) if 0 < s < 20) else ""
        out.append(f"**Sample sizes:** {'; '.join(sizes)}.{note}")
    if manifest:
        skipped = manifest.get("skipped") or {}
        line = (f"**Judge coverage:** {manifest['judged']} of {manifest['in_scope']} neutral, search-ready "
                f"tickets had an FAQ answer to grade")
        if skipped:
            items = ", ".join(f"`{cid}` ({why})" for cid, why in list(skipped.items())[:10])
            more = f" and {len(skipped) - 10} more" if len(skipped) > 10 else ""
            line += f"; not judged: {items}{more}"
        out.append(line + ".")

    if system:
        mis = system.get("misrouted") or []
        if mis:
            items = ", ".join(f"`{m['eval_case_id']}` ({m['expected']} → {m['predicted']})" for m in mis[:10])
            more = f" and {len(mis) - 10} more" if len(mis) > 10 else ""
            out.append(f"**Misrouted tickets ({len(mis)}):** {items}{more}.")
        err = system.get("errors") or {}
        if err.get("agent_call_errors"):
            types = ", ".join(f"{k}: {v}" for k, v in sorted((err.get("by_type") or {}).items()))
            routing_errors = (system.get("routing") or {}).get("confusion", {})
            n_err_pred = sum(row.get(km.ERROR, 0) for row in routing_errors.values())
            impact = (f"{n_err_pred} were sentiment calls and count as ERROR predictions in T1/T5"
                      if n_err_pred else "none were sentiment calls, so T1/T5 are unaffected")
            out.append(f"**Agent call errors:** {err['agent_call_errors']} of {err['agent_calls']} calls ({types}); "
                       f"{impact}.")
        if err.get("db_write_errors"):
            out.append(f"**DB write errors:** {err['db_write_errors']} of {err['db_writes']} writes failed "
                       "(excluded from T3 latency).")
        search_err = err.get("search_tool_errors") or {}
        if search_err:
            out.append(f"**Search tool errors:** {', '.join(f'{k}: {v}' for k, v in search_err.items())}.")
        if err.get("invalid_faq_outputs"):
            out.append(f"**Invalid FAQ outputs:** {err['invalid_faq_outputs']} answers weren't valid JSON.")
        if system["cost"].get("search_edition"):
            out.append(f"**Cost assumption:** search priced as `{system['cost']['search_edition']}` edition "
                       "(see the TODO in pricing.yaml). Judge and infrastructure cost are excluded.")

    # Only metrics with a TDD target can be "below target".
    low = []
    for c in cases:
        for kid, _, metric, thr, _ in TDD_JUDGE_KPIS:
            score = (c["metrics"].get(metric) or {}).get("score")
            if isinstance(score, (int, float)) and score < thr:
                expl = " ".join(str(c["metrics"][metric]["explanation"]).split())[:160]
                low.append(f"`{c['case_id']}` {kid} {metric}={score:g}" + (f": {expl}" if expl else ""))
    if low:
        out.append("**Judge cases below TDD target:**\n" + "\n".join(f"  - {x}" for x in low[:10]))
    return out


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------
def build_report(grade_path: Optional[str], trace_path: Optional[str], system_path: Optional[str],
                 generated_at: Optional[datetime] = None, manifest_path: Optional[str] = None) -> tuple[str, dict]:
    gdata, tdata, system = load_json(grade_path), load_json(trace_path), load_json(system_path)
    manifest = load_json(manifest_path)
    lexical = lexical_metrics(tdata)
    judge = judge_metrics(gdata)
    cases = judge_cases(gdata, lexical["case_ids"])
    kpis = quality_kpis(system, lexical, judge)
    lat_rows = latency_rows(system)
    jdg_rows = untargeted_judge_rows(judge)
    cst_rows = cost_rows(system)
    notes = findings(system, lexical, judge, cases, kpis, manifest)
    when = (generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    def rel(p):
        return f"`{os.path.relpath(p, REPO_ROOT)}`" if p else "_none found_"

    run = (system or {}).get("run") or {}
    db = (system or {}).get("db") or {}
    db_label = f"`{db.get('url')}` ({db.get('dialect')})" if db else "_none_"
    judges = judge_models()
    md = [
        "# Executive KPI & Evaluation Report",
        "",
        f"**Generated:** {when}  ",
        f"**System metrics:** {rel(system_path)}"
        + (f" ({run.get('suite')} suite, {run.get('target')}, git `{run.get('git_revision')}`, models: "
           f"{', '.join(f'{k}=`{v}`' for k, v in (run.get('models') or {}).items())})" if run else "") + "  ",
        f"**Eval database:** {db_label}  ",
        f"**Trace file:** {rel(trace_path)}  ",
        f"**Grade file:** {rel(grade_path)}  ",
        f"**Judge model:** {', '.join(f'`{j}`' for j in judges) if judges else '_unknown_'}",
        "",
        "> Every value below is computed from the artifacts listed above. Rows without a source show "
        "⚪ NOT MEASURED. Only targets written in the TDD are judged; everything else has no target.",
        "",
        "---",
        "",
        "## 1. TDD KPIs (T1, T3–T8)",
        "",
        "| # | KPI | TDD Target | Measured | Source | TDD Section | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for k in kpis:
        md.append(f"| **{k['id']}** | {k['name']} | `{k['target']}` | **{k['measured']}** | {k['source']} | "
                  f"{k['tdd']} | {BADGE[k['status']]} |")
    md += ["", "_T2 (circuit breaker) is covered by the backend unit tests, not by this eval._"]

    md += ["", "---", "", "## 2. Metrics Without a TDD / Scoping Target (reported only)", ""]
    if run:
        md += ["Measured in-process before deployment: agents via ADK Runner against Vertex AI Gemini and the "
               "Discovery Engine datastore; Model Armor redaction and ticket writes via the backend functions. "
               "Excludes Agent Runtime / Cloud Run network hops.", ""]
    md += ["### Component latency", "",
           "| Component | P50 | P90 | P95 | Max | n | Target | Source |", "|---|---|---|---|---|---|---|---|"]
    for r in lat_rows:
        md.append(f"| **{r['name']}** | {r['p50']} | {r['p90']} | **{r['p95']}** | {r['max']} | {r['n']} | "
                  f"{r['target']} | {r['source']} |")
    md += ["", "### LLM-judge metrics", "", "| Metric | Measured | Target | Source |", "|---|---|---|---|"]
    for r in jdg_rows:
        md.append(f"| **{r['name']}** | **{r['measured']}** | {r['target']} | {r['source']} |")
    md += ["", "### Tokens & cost", "", "| Metric | Measured | Target | Source |", "|---|---|---|---|"]
    for r in cst_rows:
        md.append(f"| **{r['name']}** | **{r['measured']}** | {r['target']} | {r['source']} |")
    if system:
        cost = system["cost"]
        md += ["", "| Agent | Prompt | Cached | Output | Thinking |", "|---|---|---|---|---|"]
        for agent, u in system["tokens"].items():
            md.append(f"| {agent} | {u['prompt_token_count']:,} | {u['cached_content_token_count']:,} | "
                      f"{u['candidates_token_count']:,} | {u['thoughts_token_count']:,} |")
        if cost.get("by_segment"):
            md += ["", "| Segment | Tickets | Cost per ticket | Cost per 1K |", "|---|---|---|---|"]
            for seg, s in sorted(cost["by_segment"].items()):
                md.append(f"| {seg} | {s['n']} | ${s['per_ticket_usd']:.5f} | {fmt_usd(s['per_ticket_usd'] * 1000)} |")
        md += ["", f"_{cost.get('note', '')}_"]

    if system:
        routing = system["routing"]
        labels = list(km.SENTIMENT_LABELS) + [km.INVALID, km.ERROR]
        md += ["", "---", "", "## 3. Routing Detail (T1, T5)", "",
               "| Expected \\ Predicted | " + " | ".join(labels) + " |",
               "|---|" + "---|" * len(labels)]
        for exp in km.SENTIMENT_LABELS:
            row = routing["confusion"].get(exp, {})
            md.append(f"| **{exp}** | " + " | ".join(str(row.get(p, 0)) for p in labels) + " |")
        md += ["", "| Class | Support | Precision | Recall | F1 |", "|---|---|---|---|---|"]
        for label, pc in routing["per_class"].items():
            md.append(f"| {label} | {pc['support']} | {fmt_num(pc['precision'], 3)} | "
                      f"{fmt_num(pc['recall'], 3)} | {fmt_num(pc['f1'], 3)} |")

    md += ["", "---", "", "## 4. Case-by-Case Evaluation Breakdown", ""]
    if cases:
        md += ["| Case | T8 Faithfulness | T4 Recall | T6 ROUGE-L | Scope Guard | Rubric Adherence | Hallucination |",
               "|---|---|---|---|---|---|---|"]
        for c in cases:
            m = c["metrics"]

            def sc(name):
                return (m.get(name) or {}).get("score", "-")
            rl = lexical["per_case"].get(c["case_id"])
            md.append(f"| **{c['case_id']}** | `{sc('rag_faithfulness')}` | `{sc('grounding_key_fact_recall')}` | "
                      f"`{fmt_num(rl) if rl is not None else 'N/A'}` | `{sc('rubric_scope_check')}` | "
                      f"`{sc('case_rubric_adherence')}` | `{sc('hallucination')}` |")
    else:
        md.append("_No grade file found; run `agents-cli eval grade`._")

    md += ["", "---", "", "## 5. Key Findings", ""] + [f"- {n}" for n in notes] + [""]

    report_json = {
        "generated_at": when,
        "sources": {"system_metrics": system_path, "trace": trace_path, "grade": grade_path,
                    "trace_manifest": manifest_path},
        "run": run,
        "db": db or None,
        "n_cases": (system or {}).get("n_cases"),
        "n_turns": (system or {}).get("n_turns"),
        "judge_coverage": ({k: manifest[k] for k in ("in_scope", "judged")} if manifest else None),
        "quality_kpis": kpis,
        "no_target_metrics": {"latency": lat_rows, "llm_judge": jdg_rows, "cost": cst_rows},
        "findings": notes,
        "gates_failed": [f"{k['id']} {k['name']}" for k in kpis if k["status"] == FAIL],
    }
    return "\n".join(md), report_json


# ---------------------------------------------------------------------------
# CI outputs: PR comment and GitHub annotations (rendered from report_json only)
# ---------------------------------------------------------------------------
def render_pr_comment(data: dict, run_url: Optional[str] = None) -> str:
    """Compact, report-only summary for a sticky PR comment. Starts with a marker so the
    workflow can find and update its own comment instead of adding a new one each push."""
    run = data.get("run") or {}
    head = [PR_COMMENT_MARKER, f"## 🧪 Agent eval: `{run.get('git_revision') or 'unknown revision'}`", ""]
    scope = []
    if data.get("n_cases") is not None:
        scope.append(f"**{data['n_cases']}** tickets / {data['n_turns']} turns")
    cov = data.get("judge_coverage")
    if cov:
        scope.append(f"LLM judge on {cov['judged']} of {cov['in_scope']} neutral, search-ready tickets")
    if run.get("models"):
        scope.append("models: " + ", ".join(f"{k}=`{v}`" for k, v in run["models"].items()))
    if scope:
        head.append(" · ".join(scope))
    head += ["", "_Report-only: these results do not block merging._", "",
             "**TDD KPIs**", "", "| # | KPI | Measured | TDD Target | |", "|---|---|---|---|---|"]
    for k in data["quality_kpis"]:
        head.append(f"| {k['id']} | {k['name']} | {k['measured']} | {k['target']} | {SHORT_BADGE[k['status']]} |")
    other = data["no_target_metrics"]
    head += ["", "**No TDD / Scoping target (reported only)**", "", "| Metric | Measured |", "|---|---|"]
    for r in other["latency"]:
        head.append(f"| {r['name']} P95 | {r['measured']} |")
    for r in other["llm_judge"] + other["cost"]:
        head.append(f"| {r['name']} | {r['measured']} |")
    head += ["", "<details><summary>Findings</summary>", ""] + [f"- {n}" for n in data["findings"]] + [
        "", "</details>", ""]
    if run_url:
        head.append(f"Full report, confusion matrix, latency percentiles, cost tables and per-case scores: "
                    f"[run summary]({run_url}) (artifact `eval-results`).")
    return "\n".join(head) + "\n"


def render_annotations(data: dict) -> list[str]:
    """One GitHub `::warning::` line per TDD KPI below target (shown on the PR's Checks tab)."""
    def esc(s: str) -> str:
        return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    rows = [(f"{k['id']} {k['name']}", k["measured"], k["target"]) for k in data["quality_kpis"]
            if k["status"] == FAIL]
    return [f"::warning title=KPI below TDD target::{esc(f'{n}: {m} (target {t})')}" for n, m, t in rows]


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Generate the KPI report from eval artifacts.")
    p.add_argument("--run-dir", help="one run_inference.py output dir: every section is read from it")
    p.add_argument("--system", help="system_metrics.json (default: latest under artifacts/eval)")
    p.add_argument("--trace", help="trace JSON (default: latest under artifacts/traces)")
    p.add_argument("--grade", help="grade results JSON (default: latest under artifacts/grade_results)")
    p.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "artifacts"))
    p.add_argument("--pr-comment", help="also write a compact PR comment (markdown) to this path")
    p.add_argument("--run-url", help="link to the CI run, used in the PR comment")
    p.add_argument("--annotations", action="store_true", help="print ::warning:: lines for TDD KPIs below target")
    p.add_argument("--enforce-gates", action="store_true", help="exit 1 if any measured TDD gate fails")
    args = p.parse_args(argv)

    manifest_path = None
    if args.run_dir:
        # No fallback to "latest anywhere": a missing artifact is NOT MEASURED for this run.
        paths = run_dir_paths(args.run_dir)
        system_path = args.system or paths["system"]
        trace_path = args.trace or paths["trace"]
        grade_path = args.grade or paths["grade"]
        manifest_path = paths["manifest"]
    else:
        art = os.path.join(REPO_ROOT, "artifacts")
        system_path = args.system or latest(os.path.join(art, "eval", "*", "system_metrics.json"))
        trace_path = args.trace or latest(os.path.join(art, "traces", "**", "*.json"))
        grade_path = args.grade or latest(os.path.join(art, "grade_results", "**", "*.json"))
    if not any((system_path, trace_path, grade_path)):
        print("No eval artifacts found. Run tests/eval/run_inference.py and/or agents-cli eval.", file=sys.stderr)
        return 2

    md, data = build_report(grade_path, trace_path, system_path, manifest_path=manifest_path)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "kpi_report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(args.out_dir, "kpi_report.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Generated {os.path.relpath(args.out_dir, REPO_ROOT)}/kpi_report.md and kpi_report.json")
    if args.pr_comment:
        with open(args.pr_comment, "w", encoding="utf-8") as f:
            f.write(render_pr_comment(data, args.run_url))
    if args.annotations:
        for line in render_annotations(data):
            print(line)
    if data["gates_failed"]:
        print("Failing TDD gates: " + "; ".join(data["gates_failed"]))
    return 1 if args.enforce_gates and data["gates_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
