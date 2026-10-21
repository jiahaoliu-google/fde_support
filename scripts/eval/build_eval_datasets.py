#!/usr/bin/env python3
"""
Builds the rubric-evaluation dataset from the golden set.

The answer rubric applies ONLY to tickets that are both:
  * neutral        (expected_sentiment == "NEUTRAL_FACTUAL"), and
  * ready to answer (search_ready == True)

That is segments S, O and M (whose final turn is search-ready). Urgent tickets (U)
go to the human queue and vague tickets (V) never search, so neither produces a
RAG answer and both are excluded here. They are scored elsewhere (router F1 and
search gating).

Output: tests/eval/datasets/faq_rubric_dataset.json (agents-cli EvaluationDataset).
Golden-only fields (segment, key_facts, ...) are kept so code metrics can use them.

Usage:  python3 scripts/build_eval_datasets.py
"""

import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GOLDEN = os.path.join(ROOT, "tests", "eval", "golden", "golden_set_v1.json")
OUT = os.path.join(ROOT, "tests", "eval", "datasets", "faq_rubric_dataset.json")


def in_rubric_scope(case: dict) -> bool:
    """Neutral and ready to answer. The single source of truth for rubric scope."""
    return case.get("expected_sentiment") == "NEUTRAL_FACTUAL" and case.get("search_ready") is True


def to_inference_case(case: dict) -> dict:
    out = {k: v for k, v in case.items() if k != "agent_data"}
    if "agent_data" in case:
        # Multi-turn: send the scripted user turns; the rubric scores the final answer.
        out["conversation"] = [t["prompt"] for t in case["agent_data"]["turns"]]
        out["prompt"] = case["agent_data"]["turns"][-1]["prompt"]
    # LLM-judge templates can only reference top-level fields ({key_facts}, {rubric_items});
    # dotted paths such as {rubric_groups.case_rubrics} are not resolved.
    rubrics = case.get("rubric_groups", {}).get("case_rubrics", {}).get("rubrics", [])
    out["rubric_items"] = [f"{r['rubric_id']}: {r['content']['property']['description']}" for r in rubrics]
    return out


def main() -> int:
    with open(GOLDEN) as f:
        golden = json.load(f)

    selected = [to_inference_case(c) for c in golden if in_rubric_scope(c)]
    excluded = Counter(c["segment"] for c in golden if not in_rubric_scope(c))

    # Hard guard: urgent or vague tickets must never reach the rubric judge.
    leaked = [c["eval_case_id"] for c in selected if c["segment"] in ("U", "V")]
    if leaked:
        print(f"ERROR: out-of-scope cases leaked into rubric dataset: {leaked}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"eval_cases": selected}, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(selected)} cases to {os.path.relpath(OUT, ROOT)}")
    print(f"  included by segment: {dict(Counter(c['segment'] for c in selected))}")
    print(f"  excluded by segment: {dict(excluded)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
