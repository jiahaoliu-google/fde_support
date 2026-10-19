"""Deterministic KPI math for the pre-deployment agent eval.

Pure functions only (no network, no ADK imports), so they are unit-tested in CI and
shared by tests/eval/run_inference.py (which produces the numbers) and
scripts/generate_kpi_report.py (which renders them).
"""
from __future__ import annotations

import math
import os
from typing import Any, Iterable, Optional

import yaml

URGENT = "URGENT_FRUSTRATED"
NEUTRAL = "NEUTRAL_FACTUAL"
SENTIMENT_LABELS = (URGENT, NEUTRAL)
# Predictions that are not a valid label. Both count as wrong for accuracy and F1.
INVALID = "INVALID"   # agent answered, but not with a usable label
ERROR = "ERROR"       # agent call failed (exception / timeout)

# Gate targets. Routing comes from the TDD; the system targets are the *proposed*
# values in docs/EVALUATION_PLAN.md (M1, M5, M6). The TDD sets no latency/cost targets.
KPI_TARGETS = {
    "routing_accuracy": 0.95,      # TDD North Star
    "urgent_f1": 0.95,             # TDD CI gate
    "e2e_latency_p95_s": 3.0,      # proposed (M1)
    "search_latency_p95_s": 1.2,   # proposed (M5)
    "error_rate": 0.0,             # proposed (M6)
}

DEFAULT_PRICING_PATH = os.path.join(os.path.dirname(__file__), "pricing.yaml")

TOKEN_FIELDS = (
    "prompt_token_count",
    "cached_content_token_count",
    "candidates_token_count",
    "thoughts_token_count",
    "total_token_count",
)


# ---------------------------------------------------------------------------
# Classification (routing accuracy, per-class precision / recall / F1)
# ---------------------------------------------------------------------------
def _safe_div(num: float, den: float) -> Optional[float]:
    return num / den if den else None


def classification_report(pairs: Iterable[tuple[str, str]]) -> dict:
    """Routing metrics from (expected_label, predicted_label) pairs.

    Predictions outside SENTIMENT_LABELS (INVALID / ERROR) are wrong for accuracy and
    are false negatives for the expected class. F1 is None when it's undefined (no
    expected or predicted examples of that class), never silently 0 or 1.
    """
    pairs = list(pairs)
    n = len(pairs)
    confusion: dict[str, dict[str, int]] = {}
    for expected, predicted in pairs:
        row = confusion.setdefault(expected, {})
        row[predicted] = row.get(predicted, 0) + 1

    per_class = {}
    for label in SENTIMENT_LABELS:
        tp = sum(1 for e, p in pairs if e == label and p == label)
        fp = sum(1 for e, p in pairs if e != label and p == label)
        fn = sum(1 for e, p in pairs if e == label and p != label)
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        if precision is None or recall is None:
            f1 = None
        elif precision + recall == 0:
            f1 = 0.0
        else:
            f1 = 2 * precision * recall / (precision + recall)
        per_class[label] = {
            "support": tp + fn, "tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "recall": recall, "f1": f1,
        }

    correct = sum(1 for e, p in pairs if e == p)
    return {
        "n": n,
        "accuracy": _safe_div(correct, n),
        "per_class": per_class,
        "confusion": confusion,
        "invalid_predictions": sum(1 for _, p in pairs if p == INVALID),
        "error_predictions": sum(1 for _, p in pairs if p == ERROR),
    }


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------
def percentile(values: Iterable[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile (same as numpy's default). None for no data."""
    data = sorted(values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (pct / 100.0) * (len(data) - 1)
    lo, hi = math.floor(rank), math.ceil(rank)
    return data[lo] + (data[hi] - data[lo]) * (rank - lo)


def latency_summary(values: Iterable[float]) -> Optional[dict]:
    data = [v for v in values if v is not None]
    if not data:
        return None
    return {
        "n": len(data),
        "mean": sum(data) / len(data),
        "p50": percentile(data, 50),
        "p90": percentile(data, 90),
        "p95": percentile(data, 95),
        "max": max(data),
    }


# ---------------------------------------------------------------------------
# Tokens and cost
# ---------------------------------------------------------------------------
def empty_usage() -> dict:
    return {f: 0 for f in TOKEN_FIELDS}


def usage_from_metadata(usage_metadata: Any) -> Optional[dict]:
    """Token counts from a Gemini usage_metadata object or dict (None fields count as 0)."""
    if usage_metadata is None:
        return None
    get = usage_metadata.get if isinstance(usage_metadata, dict) else (lambda k: getattr(usage_metadata, k, None))
    return {f: int(get(f) or 0) for f in TOKEN_FIELDS}


def add_usage(total: dict, usage: Optional[dict]) -> dict:
    if usage:
        for f in TOKEN_FIELDS:
            total[f] = total.get(f, 0) + int(usage.get(f, 0) or 0)
    return total


def load_pricing(path: str = DEFAULT_PRICING_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def model_call_cost(model: str, usage: dict, pricing: dict) -> Optional[float]:
    """USD for one model's token usage, per the formula in pricing.yaml.

    prompt_token_count already includes cached tokens, and thinking tokens bill as
    output. Returns None if the model or any of its rates is missing, so an unknown
    price shows up as "not measured" instead of $0.
    """
    rates = (pricing.get("models") or {}).get(model)
    if not rates or any(rates.get(k) is None for k in ("input", "output", "cached_input")):
        return None
    cached = usage.get("cached_content_token_count", 0)
    uncached_input = max(usage.get("prompt_token_count", 0) - cached, 0)
    output = usage.get("candidates_token_count", 0) + usage.get("thoughts_token_count", 0)
    return (uncached_input * rates["input"] + cached * rates["cached_input"] + output * rates["output"]) / 1e6


def search_cost(n_queries: int, pricing: dict) -> Optional[float]:
    search = pricing.get("search") or {}
    rate = (search.get("per_1k_queries") or {}).get(search.get("edition"))
    if rate is None:
        return None
    return n_queries * rate / 1000.0


def gate(value: Optional[float], target: float, higher_is_better: bool = True) -> Optional[bool]:
    """PASS/FAIL against a target; None when the value wasn't measured."""
    if value is None:
        return None
    return value >= target if higher_is_better else value <= target
