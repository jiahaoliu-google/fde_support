#!/usr/bin/env python3
"""
Generate kpi_report.md / kpi_report.json from evaluation artifacts.

Every number in the report is computed from a run artifact. Anything without a source
artifact is shown as NOT MEASURED, never as a default value:
  * routing, latency, error rate, tokens, cost:  system_metrics.json (tests/eval/run_inference.py)
  * ROUGE-L, corpus BLEU-4:                      traces.json (same run) or artifacts/traces/*.json
  * LLM-judge metrics:                           grade results JSON (agents-cli eval grade)

Usage:
  python scripts/generate_kpi_report.py --run-dir artifacts/eval/<ts>   # all sections from one run (CI)
  python scripts/generate_kpi_report.py                 # latest artifact of each kind
  python scripts/generate_kpi_report.py --system artifacts/eval/<ts>/system_metrics.json
  python scripts/generate_kpi_report.py --enforce-gates # exit 1 if a measured gate fails
  ... --pr-comment out.md --run-url <url>               # compact table for a PR comment
  ... --annotations                                     # ::warning:: per KPI below target
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime
from typing import Any, Optional

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import yaml  # noqa: E402

from tests.eval.static_rubric_evaluator import TDD_GATES, evaluate_static_rubric, corpus_bleu  # noqa: E402
from tests.eval import kpi_metrics as km  # noqa: E402

PASS, FAIL, NOT_MEASURED = "PASS", "FAIL", "NOT MEASURED"
BADGE = {PASS: "🟢 **PASS**", FAIL: "🔴 **FAIL**", NOT_MEASURED: "⚪ NOT MEASURED", "INFO": "ℹ️ INFO"}
SHORT_BADGE = {PASS: "✅", FAIL: "❌", NOT_MEASURED: "⚪", "INFO": "ℹ️"}
PR_COMMENT_MARKER = "<!-- fde-support-agent-eval -->"
DEFAULT_EVAL_CONFIG = os.path.join(REPO_ROOT, "tests", "eval", "eval_config_faq_rubric.yaml")

# LLM-judge gates. TDD-sourced values come from static_rubric_evaluator.TDD_GATES so the
# report and the evaluator can't drift apart; the rest are project targets.
JUDGE_KPIS = [
    # (id, name, metric_name, threshold, gate, source of target)
    ("4", "Grounding Key Fact Recall", "grounding_key_fact_recall", TDD_GATES["grounding_recall"], "Nightly", "TDD"),
    ("8", "RAG Faithfulness & Semantic QA", "rag_faithfulness", TDD_GATES["faithfulness"], "Nightly", "TDD"),
    ("Scope", "Rubric Scope Guard (Neutral Only)", "rubric_scope_check", 1.0, "PR", "project"),
    ("Rubric", "Case Rubric Adherence", "case_rubric_adherence", 0.85, "Nightly", "project"),
    ("Hallucination", "Zero Hallucination Rate", "hallucination_v1", 1.0, "PR", "project"),
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


def judge_metrics(gdata: Optional[dict]) -> dict:
    """{metric_name: {"mean": float|None, "n": int}} from the grade file summary."""
    out = {}
    for m in (gdata or {}).get("summary_metrics", []):
        valid = m.get("num_cases_valid") or 0
        out[m.get("metric_name")] = {
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
                metrics[name] = {"score": val.get("score"), "explanation": val.get("explanation") or ""}
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


def fmt_usd(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:.2f}"


# ---------------------------------------------------------------------------
# KPI rows
# ---------------------------------------------------------------------------
def quality_kpis(system: Optional[dict], lexical: dict, judge: dict) -> list[dict]:
    rows = []
    routing = (system or {}).get("routing")
    sys_src = f"router eval, n={routing['n']}" if routing else RUN_ROUTER_HINT
    acc = routing["accuracy"] if routing else None
    f1 = routing["per_class"][km.URGENT]["f1"] if routing else None
    rows.append({"id": "1", "name": "Routing Accuracy (TDD)", "target": f"≥ {km.KPI_TARGETS['routing_accuracy']:.0%}",
                 "value": acc, "measured": fmt_pct(acc), "gate": "PR", "source": sys_src,
                 "status": status(km.gate(acc, km.KPI_TARGETS["routing_accuracy"]))})
    rows.append({"id": "5", "name": "Urgent F1 Score (TDD)", "target": f"≥ {km.KPI_TARGETS['urgent_f1']:.2f}",
                 "value": f1, "measured": fmt_num(f1, 3), "gate": "PR", "source": sys_src,
                 "status": status(km.gate(f1, km.KPI_TARGETS["urgent_f1"]))})

    lex_src = f"trace file, n={lexical['n']}" if lexical["n"] else "no trace file"
    rows.append({"id": "6", "name": "ROUGE-L vs Historical Answer (TDD)", "target": f"≥ {TDD_GATES['rouge_l']:.2f}",
                 "value": lexical["rouge_l"], "measured": fmt_num(lexical["rouge_l"]), "gate": "PR", "source": lex_src,
                 "status": status(km.gate(lexical["rouge_l"], TDD_GATES["rouge_l"]))})
    rows.append({"id": "7", "name": "Corpus BLEU-4 Score (TDD)", "target": f"≥ {TDD_GATES['bleu']:.2f}",
                 "value": lexical["bleu4"], "measured": fmt_num(lexical["bleu4"]), "gate": "PR", "source": lex_src,
                 "status": status(km.gate(lexical["bleu4"], TDD_GATES["bleu"]))})

    for kid, name, metric, threshold, gate_type, origin in JUDGE_KPIS:
        m = judge.get(metric) or {"mean": None, "n": 0, "errors": 0}
        target = "100%" if threshold == 1.0 else f"≥ {threshold:.0%}"
        if origin == "project":
            target += " (project)"
        src = f"LLM judge, n={m['n']}" if m["n"] else "no grade file"
        if m.get("errors"):
            src += f", {m['errors']} judge errors"
        rows.append({"id": kid, "name": name, "target": target, "value": m["mean"],
                     "measured": fmt_pct(m["mean"]), "gate": gate_type, "source": src,
                     "status": status(km.gate(m["mean"], threshold))})
    return rows


def system_rows(system: Optional[dict]) -> list[dict]:
    if not system:
        return [{"name": n, "measured": "—", "target": t, "status": NOT_MEASURED, "source": RUN_ROUTER_HINT}
                for n, t in (("Agent Latency per Turn (P95)", "≤ 3.0 s (proposed)"),
                             ("Search Tool Latency (P95)", "≤ 1.2 s (proposed)"),
                             ("Agent Call Error Rate", "0.0% (proposed)"),
                             ("Est. Cost per 1K Tickets", "Report only"))]
    lat = system["latency_s"]
    t = system["targets"]
    g = system["gates"]
    router_only = system["run"].get("suite") == "router"
    e2e, search = lat.get("e2e_turn"), lat.get("search_tool")
    err = system["errors"]
    cost = system["cost"]
    n_turns = system.get("n_turns")

    def dist(d):
        return "" if not d else f" (P50 {fmt_s(d['p50'])}, max {fmt_s(d['max'])}, n={d['n']})"

    rows = [
        {"name": "Agent Latency per Turn (P95)" + (" — router only" if router_only else ""),
         "measured": fmt_s(e2e and e2e["p95"]) + dist(e2e),
         "target": f"≤ {t['e2e_latency_p95_s']:.1f} s (proposed)", "status": status(g["e2e_latency_p95_s"]),
         "source": f"{n_turns} turns"},
        {"name": "Sentiment Agent Latency (P95)", "measured": fmt_s((lat.get("sentiment") or {}).get("p95"))
         + dist(lat.get("sentiment")), "target": "Diagnostic", "status": "INFO", "source": "per call"},
        {"name": "FAQ Agent Latency (P95)",
         "measured": "not run (router suite)" if router_only else fmt_s((lat.get("faq") or {}).get("p95")) + dist(lat.get("faq")),
         "target": "Diagnostic", "status": NOT_MEASURED if router_only else "INFO", "source": "per call"},
        {"name": "Search Tool Latency (P95)",
         "measured": "not run (router suite)" if router_only else fmt_s(search and search["p95"]) + dist(search),
         "target": f"≤ {t['search_latency_p95_s']:.1f} s (proposed)", "status": status(g["search_latency_p95_s"]),
         "source": f"{err['search_calls']} search calls"},
        {"name": "Agent Call Error Rate",
         "measured": f"{fmt_pct(err['error_rate'])} ({err['agent_call_errors']}/{err['agent_calls']})",
         "target": f"{t['error_rate']:.1%} (proposed)", "status": status(g["error_rate"]),
         "source": ", ".join(f"{k}: {v}" for k, v in sorted(err["by_type"].items())) or "no errors"},
        {"name": "Est. Cost per 1K Tickets" + (" — router only" if router_only else ""),
         "measured": fmt_usd(cost["per_1k_tickets_usd"]),
         "target": "Report only", "status": "INFO" if cost["per_1k_tickets_usd"] is not None else NOT_MEASURED,
         "source": "; ".join(cost["unpriced_reasons"]) or f"pricing.yaml, search edition={cost.get('search_edition')}"},
    ]
    return rows


# ---------------------------------------------------------------------------
# Findings (generated from the data, never written by hand)
# ---------------------------------------------------------------------------
def findings(system: Optional[dict], lexical: dict, judge: dict, cases: list[dict],
             kpis: list[dict], sys_rows: list[dict], manifest: Optional[dict] = None) -> list[str]:
    out = []
    failing = [f"{k['name']} ({k['measured']} vs {k['target']})" for k in kpis if k["status"] == FAIL]
    failing += [f"{r['name']} ({r['measured']} vs {r['target']})" for r in sys_rows if r["status"] == FAIL]
    missing = [k["name"] for k in kpis if k["status"] == NOT_MEASURED]
    out.append(f"**Failing gates:** {'; '.join(failing)}." if failing else "**Failing gates:** none of the measured gates fail.")
    if missing:
        out.append(f"**Not measured:** {', '.join(missing)}.")

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
        slow = system.get("slowest_turns") or []
        if slow:
            items = ", ".join(f"`{s['eval_case_id']}` turn {s['turn']} {s['latency_s']:.2f} s" for s in slow[:3])
            out.append(f"**Slowest turns:** {items}.")
        search_err = system["errors"].get("search_tool_errors") or {}
        if search_err:
            out.append(f"**Search tool errors:** {', '.join(f'{k}: {v}' for k, v in search_err.items())}.")
        if system["errors"].get("invalid_faq_outputs"):
            out.append(f"**Invalid FAQ outputs:** {system['errors']['invalid_faq_outputs']} answers weren't valid JSON.")
        if system["cost"].get("search_edition"):
            out.append(f"**Cost assumption:** search priced as `{system['cost']['search_edition']}` edition "
                       "(see the TODO in pricing.yaml). Judge and infrastructure cost are excluded.")

    thresholds = {metric: thr for _, _, metric, thr, _, _ in JUDGE_KPIS}
    low = []
    for c in cases:
        for metric, thr in thresholds.items():
            score = (c["metrics"].get(metric) or {}).get("score")
            if isinstance(score, (int, float)) and score < thr:
                expl = " ".join(str(c["metrics"][metric]["explanation"]).split())[:160]
                low.append(f"`{c['case_id']}` {metric}={score:g}" + (f": {expl}" if expl else ""))
    if low:
        out.append("**Judge cases below target:**\n" + "\n".join(f"  - {x}" for x in low[:10]))
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
    sys_rows = system_rows(system)
    notes = findings(system, lexical, judge, cases, kpis, sys_rows, manifest)
    when = (generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    def rel(p):
        return f"`{os.path.relpath(p, REPO_ROOT)}`" if p else "_none found_"

    run = (system or {}).get("run") or {}
    judges = judge_models()
    md = [
        "# Executive KPI & Evaluation Report",
        "",
        f"**Generated:** {when}  ",
        f"**System metrics:** {rel(system_path)}"
        + (f" ({run.get('suite')} suite, {run.get('target')}, git `{run.get('git_revision')}`, models: "
           f"{', '.join(f'{k}=`{v}`' for k, v in (run.get('models') or {}).items())})" if run else "") + "  ",
        f"**Trace file:** {rel(trace_path)}  ",
        f"**Grade file:** {rel(grade_path)}  ",
        f"**Judge model:** {', '.join(f'`{j}`' for j in judges) if judges else '_unknown_'}",
        "",
        "> Every value below is computed from the artifacts listed above. Rows without a source show "
        "⚪ NOT MEASURED.",
        "",
        "---",
        "",
        "## 1. Core Quality & Routing KPIs",
        "",
        "| # | KPI / Metric Name | Target | Measured Value | Source | Gate Type | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for k in kpis:
        md.append(f"| **{k['id']}** | {k['name']} | `{k['target']}` | **{k['measured']}** | {k['source']} | "
                  f"{k['gate']} | {BADGE[k['status']]} |")

    md += ["", "---", "", "## 2. Deterministic System & Performance Metrics", ""]
    if run:
        md += [f"Measured in-process before deployment (ADK Runner against Vertex AI Gemini and the "
               f"Discovery Engine datastore). Excludes Agent Runtime / Cloud Run network hops, Model Armor "
               f"and Cloud SQL writes.", ""]
    md += ["| Metric | Measured Value | Target | Source | Status |", "|---|---|---|---|---|"]
    for r in sys_rows:
        md.append(f"| **{r['name']}** | **{r['measured']}** | {r['target']} | {r['source']} | {BADGE[r['status']]} |")

    if system:
        routing = system["routing"]
        labels = list(km.SENTIMENT_LABELS) + [km.INVALID, km.ERROR]
        md += ["", "### Routing detail", "",
               "| Expected \\ Predicted | " + " | ".join(labels) + " |",
               "|---|" + "---|" * len(labels)]
        for exp in km.SENTIMENT_LABELS:
            row = routing["confusion"].get(exp, {})
            md.append(f"| **{exp}** | " + " | ".join(str(row.get(p, 0)) for p in labels) + " |")
        md += ["", "| Class | Support | Precision | Recall | F1 |", "|---|---|---|---|---|"]
        for label, pc in routing["per_class"].items():
            md.append(f"| {label} | {pc['support']} | {fmt_num(pc['precision'], 3)} | "
                      f"{fmt_num(pc['recall'], 3)} | {fmt_num(pc['f1'], 3)} |")

        cost = system["cost"]
        tok = system["tokens"]
        md += ["", "### Tokens & cost", "",
               "| Agent | Prompt | Cached | Output | Thinking |", "|---|---|---|---|---|"]
        for agent, u in tok.items():
            md.append(f"| {agent} | {u['prompt_token_count']:,} | {u['cached_content_token_count']:,} | "
                      f"{u['candidates_token_count']:,} | {u['thoughts_token_count']:,} |")
        if cost.get("by_segment"):
            md += ["", "| Segment | Tickets | Cost per ticket | Cost per 1K |", "|---|---|---|---|"]
            for seg, s in sorted(cost["by_segment"].items()):
                md.append(f"| {seg} | {s['n']} | ${s['per_ticket_usd']:.5f} | {fmt_usd(s['per_ticket_usd'] * 1000)} |")
        md += ["", f"_{cost.get('note', '')}_"]

    md += ["", "---", "", "## 3. Case-by-Case Evaluation Breakdown", ""]
    if cases:
        md += ["| Case | Scope Guard | Faithfulness | Recall | Rubric Adherence | Zero Hallucination | ROUGE-L |",
               "|---|---|---|---|---|---|---|"]
        for c in cases:
            m = c["metrics"]

            def sc(name):
                return (m.get(name) or {}).get("score", "-")
            rl = lexical["per_case"].get(c["case_id"])
            md.append(f"| **{c['case_id']}** | `{sc('rubric_scope_check')}` | `{sc('rag_faithfulness')}` | "
                      f"`{sc('grounding_key_fact_recall')}` | `{sc('case_rubric_adherence')}` | "
                      f"`{sc('hallucination_v1')}` | `{fmt_num(rl) if rl is not None else 'N/A'}` |")
    else:
        md.append("_No grade file found; run `agents-cli eval grade`._")

    md += ["", "---", "", "## 4. Key Findings", ""] + [f"- {n}" for n in notes] + [""]

    report_json = {
        "generated_at": when,
        "sources": {"system_metrics": system_path, "trace": trace_path, "grade": grade_path,
                    "trace_manifest": manifest_path},
        "run": run,
        "n_cases": (system or {}).get("n_cases"),
        "n_turns": (system or {}).get("n_turns"),
        "judge_coverage": ({k: manifest[k] for k in ("in_scope", "judged")} if manifest else None),
        "quality_kpis": kpis,
        "system_metrics": sys_rows,
        "findings": notes,
        "gates_failed": [k["name"] for k in kpis if k["status"] == FAIL]
                        + [r["name"] for r in sys_rows if r["status"] == FAIL],
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
             "| KPI | Measured | Target | |", "|---|---|---|---|"]
    for k in data["quality_kpis"]:
        head.append(f"| {k['name']} | {k['measured']} | {k['target']} | {SHORT_BADGE[k['status']]} |")
    for r in data["system_metrics"]:
        if r["status"] == "INFO" and "Cost" not in r["name"]:
            continue  # per-agent diagnostics live in the full report
        measured = r["measured"].split(" (")[0]
        head.append(f"| {r['name']} | {measured} | {r['target']} | {SHORT_BADGE[r['status']]} |")
    head += ["", "<details><summary>Findings</summary>", ""] + [f"- {n}" for n in data["findings"]] + [
        "", "</details>", ""]
    if run_url:
        head.append(f"Full report, confusion matrix, cost tables and per-case scores: [run summary]({run_url}) "
                    "(artifact `eval-results`).")
    return "\n".join(head) + "\n"


def render_annotations(data: dict) -> list[str]:
    """One GitHub `::warning::` line per KPI below target (shown on the PR's Checks tab)."""
    def esc(s: str) -> str:
        return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    rows = [(k["name"], k["measured"], k["target"]) for k in data["quality_kpis"] if k["status"] == FAIL]
    rows += [(r["name"], r["measured"].split(" (")[0], r["target"]) for r in data["system_metrics"]
             if r["status"] == FAIL]
    return [f"::warning title=KPI below target::{esc(f'{n}: {m} (target {t})')}" for n, m, t in rows]


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Generate the KPI report from eval artifacts.")
    p.add_argument("--run-dir", help="one run_inference.py output dir: every section is read from it")
    p.add_argument("--system", help="system_metrics.json (default: latest under artifacts/eval)")
    p.add_argument("--trace", help="trace JSON (default: latest under artifacts/traces)")
    p.add_argument("--grade", help="grade results JSON (default: latest under artifacts/grade_results)")
    p.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "artifacts"))
    p.add_argument("--pr-comment", help="also write a compact PR comment (markdown) to this path")
    p.add_argument("--run-url", help="link to the CI run, used in the PR comment")
    p.add_argument("--annotations", action="store_true", help="print ::warning:: lines for KPIs below target")
    p.add_argument("--enforce-gates", action="store_true", help="exit 1 if any measured gate fails")
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
        print("Failing gates: " + "; ".join(data["gates_failed"]))
    return 1 if args.enforce_gates and data["gates_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
