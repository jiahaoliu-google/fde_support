#!/usr/bin/env python3
"""
TDD-aligned static rubric evaluator for FAQ Agent answers.

Gates taken from jliu_support_tdd.pdf ("Testing & Evaluation Framework"):
  - BLEU >= 0.30 and ROUGE-L >= 0.35 vs. the historical Twitter agent reply (PR gate)
  - RAG faithfulness >= 90%, judged by Gemini 3.5 Flash; covers hallucinations
    *and* agent policy violations (Nightly gate)
  - Grounding recall >= 85% (North Star metric)

Everything else here (the policy checks list, the composite weights) is a
project-level choice, not a TDD requirement, and is labelled as such.

Scope (only neutral, search-ready tickets that actually produce a RAG answer):
  - Segment U: not applicable (routed to the human queue; scored by router F1)
  - Segment V: not applicable (no search; scored by search-gating G1)
  - Segments S, O and turn 2 of M: applicable
  - Lexical gates (BLEU / ROUGE-L) use only S and M, because those have a real
    historical brand reply. O references are synthetic, so they are excluded.

Faithfulness and key-fact recall need semantic judgement. Pass the LLM-judge
scores in via `judge_scores`; without them the evaluator falls back to cheap
lexical proxies and marks the result `"source": "lexical_proxy"` so a proxy
is never mistaken for the real gate.

Pure Python, no third-party dependencies.
"""

import html
import json
import math
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence

# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------
TDD_GATES = {
    "faithfulness": 0.90,      # TDD Nightly semantic QA
    "grounding_recall": 0.85,  # TDD North Star
    "rouge_l": 0.35,           # TDD PR gate
    "bleu": 0.30,              # TDD PR gate (corpus-level)
}

# Project choice (not from the TDD): informational composite only.
COMPOSITE_WEIGHTS = {
    "faithfulness": 0.35,
    "grounding_recall": 0.30,
    "policy_compliance": 0.25,
    "rouge_l": 0.10,
}

APPLICABLE_SEGMENTS = {"S", "O", "M"}
LEXICAL_SEGMENTS = {"S", "M"}

# --------------------------------------------------------------------------
# Text normalization (applied to both candidate and reference before BLEU/ROUGE)
# --------------------------------------------------------------------------
_HANDLE_RE = re.compile(r"@\w+")
_URL_RE = re.compile(r"https?://\S+")
_SIGNOFF_RE = re.compile(r"(\^\s?[A-Za-z]{1,4}\b)|(\s[-–*~][A-Z]{1,4}\s*$)")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def normalize(text: str) -> str:
    """Decode entities; strip @handles, URLs and agent sign-offs (e.g. '^AB', '-JR')."""
    text = html.unescape(text or "")
    text = _HANDLE_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _SIGNOFF_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> List[str]:
    """Lower-cased word tokens. Emoji and punctuation are dropped."""
    return _TOKEN_RE.findall(normalize(text).lower().replace("\u2019", "'"))


# --------------------------------------------------------------------------
# ROUGE-L (LCS-based F1, as in Lin 2004 with beta = 1)
# --------------------------------------------------------------------------
def _lcs_length(a: Sequence[str], b: Sequence[str]) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, start=1):
            cur[j] = prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


def rouge_l(candidate: str, reference: str) -> float:
    c, r = tokenize(candidate), tokenize(reference)
    lcs = _lcs_length(c, r)
    if lcs == 0:
        return 0.0
    precision, recall = lcs / len(c), lcs / len(r)
    return 2 * precision * recall / (precision + recall)


# --------------------------------------------------------------------------
# BLEU (Papineni et al. 2002; 4-gram, uniform weights, brevity penalty)
# --------------------------------------------------------------------------
def _ngrams(tokens: Sequence[str], n: int) -> Counter:
    return Counter(tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1))


def _bleu_stats(c: Sequence[str], r: Sequence[str], max_n: int = 4):
    matches, totals = [], []
    for n in range(1, max_n + 1):
        cand, ref = _ngrams(c, n), _ngrams(r, n)
        matches.append(sum(min(cnt, ref[g]) for g, cnt in cand.items()))
        totals.append(max(len(c) - n + 1, 0))
    return matches, totals


def _bleu_from_stats(matches, totals, cand_len, ref_len, smooth: bool) -> float:
    if cand_len == 0:
        return 0.0
    log_p = 0.0
    for i, (m, t) in enumerate(zip(matches, totals)):
        if smooth and i > 0:  # add-one smoothing for n >= 2 (Lin & Och 2004)
            m, t = m + 1, t + 1
        if m == 0 or t == 0:
            return 0.0
        log_p += math.log(m / t) / len(matches)
    bp = 1.0 if cand_len > ref_len else math.exp(1 - ref_len / cand_len)
    return bp * math.exp(log_p)


def sentence_bleu(candidate: str, reference: str) -> float:
    """Smoothed per-case BLEU. Diagnostic only; the TDD gate uses corpus_bleu()."""
    c, r = tokenize(candidate), tokenize(reference)
    m, t = _bleu_stats(c, r)
    return _bleu_from_stats(m, t, len(c), len(r), smooth=True)


def corpus_bleu(candidates: Iterable[str], references: Iterable[str]) -> float:
    """Corpus-level BLEU: n-gram counts pooled over all cases before dividing."""
    tot_m, tot_t, c_len, r_len = [0] * 4, [0] * 4, 0, 0
    for cand, ref in zip(candidates, references):
        c, r = tokenize(cand), tokenize(ref)
        m, t = _bleu_stats(c, r)
        tot_m = [a + b for a, b in zip(tot_m, m)]
        tot_t = [a + b for a, b in zip(tot_t, t)]
        c_len, r_len = c_len + len(c), r_len + len(r)
    return _bleu_from_stats(tot_m, tot_t, c_len, r_len, smooth=False)


# --------------------------------------------------------------------------
# Lexical proxies (used only when no LLM-judge score is supplied)
# --------------------------------------------------------------------------
_STOPWORDS = {
    "the", "and", "for", "you", "your", "with", "that", "this", "are", "can",
    "have", "will", "our", "from", "please", "would", "could", "about", "what",
}


def _content_words(text: str) -> List[str]:
    return [w for w in tokenize(text) if len(w) > 3 and w not in _STOPWORDS]


def context_support_proxy(answer: str, context_snippets: List[str]) -> float:
    """Share of the answer's content words that appear in the retrieved context."""
    words = _content_words(answer)
    if not words:
        return 1.0
    ctx = set(tokenize(" ".join(context_snippets)))
    return sum(1 for w in words if w in ctx) / len(words)


def key_fact_recall_proxy(answer: str, key_facts: List[str]) -> float:
    """A fact counts as recalled if >= 50% of its content words occur in the answer."""
    if not key_facts:
        return 1.0
    ans = set(tokenize(answer))
    recalled = 0
    for fact in key_facts:
        fw = _content_words(fact)
        if not fw or sum(1 for w in fw if w in ans) / len(fw) >= 0.5:
            recalled += 1
    return recalled / len(key_facts)


# --------------------------------------------------------------------------
# Policy checks. These come from the FAQ agent's own instructions
# (agents/faq_assistant/agent.py, STEP 4/5) and count as "agent policy
# violations" under the TDD faithfulness definition.
# --------------------------------------------------------------------------
_POLICY_CHECKS = {
    "doc_id_in_answer": re.compile(r"\btwcs_\d+\b", re.I),
    "retrieval_disclosure": re.compile(
        r"\b(knowledge base|search results?|support records?|our records|datastore|"
        r"database|past (conversations|tickets)|other (users|customers) (have )?reported)\b",
        re.I,
    ),
    "lazy_dm_redirect": re.compile(r"^\W*(please\s+)?(dm|pm|direct message|send us a dm)\b[^.?!]*[.?!]?\W*$", re.I),
    # Output-side PII leak. The TDD only requires redaction of *inputs*
    # (Model Armor); checking outputs too is a project-level addition.
    "pii_email": re.compile(r"[\w.%+-]+@[\w.-]+\.[a-z]{2,}", re.I),
    "pii_ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "pii_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
}

_OOC_FALLBACK_RE = re.compile(
    r"(don'?t have (specific )?information|couldn'?t find|could not find|no documented|"
    r"connect you with a (human|support))",
    re.I,
)


def policy_violations(answer: str) -> List[str]:
    text = answer.replace("\u2019", "'")
    return [name for name, rx in _POLICY_CHECKS.items() if rx.search(text)]


# --------------------------------------------------------------------------
# Per-case evaluation
# --------------------------------------------------------------------------
def evaluate_static_rubric(
    case: Dict[str, Any],
    generated_answer: str,
    context_snippets: List[str],
    searched: bool,
    grounded: bool,
    escalate: Optional[bool] = None,
    judge_scores: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Score one FAQ answer.

    judge_scores (optional): {"faithfulness": 0-1, "grounding_recall": 0-1} from the
    Gemini 3.5 Flash judge. When absent, lexical proxies are used and flagged.
    For Segment M, pass the turn-2 answer.
    """
    segment = case.get("segment", "S")
    neutral = case.get("expected_sentiment", "NEUTRAL_FACTUAL") == "NEUTRAL_FACTUAL"
    ready = case.get("search_ready", segment in APPLICABLE_SEGMENTS) is True
    if not (neutral and ready and segment in APPLICABLE_SEGMENTS):
        reason = {
            "U": "Urgent ticket: routed to the human queue, no AI answer. Scored by router F1.",
            "V": "Vague ticket: no search, clarifying question only. Scored by search-gating G1.",
        }.get(segment, "Rubric applies only to neutral, search-ready tickets.")
        return {"applicable": False, "segment": segment, "reason": reason}

    judge_scores = judge_scores or {}
    reference = (case.get("reference", {}).get("response", {}).get("parts") or [{}])[0].get("text", "")
    violations = policy_violations(generated_answer)

    # 1. Faithfulness (TDD: hallucinations + agent policy violations)
    if segment == "O":
        handled = bool(_OOC_FALLBACK_RE.search(generated_answer)) and not grounded and escalate is not False
        faith = {"score": 1.0 if handled else 0.0, "source": "rule_out_of_corpus",
                 "details": "Out-of-corpus: must admit no answer, grounded=false, escalate=true."}
    elif "faithfulness" in judge_scores:
        faith = {"score": float(judge_scores["faithfulness"]), "source": "llm_judge"}
    else:
        faith = {"score": round(context_support_proxy(generated_answer, context_snippets), 3),
                 "source": "lexical_proxy",
                 "details": "Lexical overlap with retrieved context. Not a substitute for the judge."}
    if violations and faith["score"] > 0:
        faith["details"] = f"Policy violations force a fail: {violations}"
        faith["score"] = 0.0
    faith["pass"] = faith["score"] >= TDD_GATES["faithfulness"]

    # 2. Grounding recall (TDD North Star). Not meaningful for O cases.
    if segment == "O":
        recall = {"score": None, "pass": None, "source": "n/a", "details": "No key facts for out-of-corpus cases."}
    elif "grounding_recall" in judge_scores:
        s = float(judge_scores["grounding_recall"])
        recall = {"score": s, "pass": s >= TDD_GATES["grounding_recall"], "source": "llm_judge"}
    else:
        s = round(key_fact_recall_proxy(generated_answer, case.get("key_facts", [])), 3)
        recall = {"score": s, "pass": s >= TDD_GATES["grounding_recall"], "source": "lexical_proxy"}

    # 3. Policy compliance (from agent instructions; see _POLICY_CHECKS)
    policy = {"score": 0.0 if violations else 1.0, "pass": not violations, "violations": violations}

    # 4. Lexical similarity (optional target, informational only). Only S/M have a real historical reply.
    if segment in LEXICAL_SEGMENTS and reference:
        rl = round(rouge_l(generated_answer, reference), 4)
        lexical = {"rouge_l": rl, "rouge_l_pass": None,
                   "sentence_bleu": round(sentence_bleu(generated_answer, reference), 4),
                   "note": "Informational only (optional target, not gated)."}
    else:
        lexical = {"rouge_l": None, "rouge_l_pass": None, "sentence_bleu": None,
                   "note": "Excluded: no historical reference for this segment."}

    # Informational composite (project choice). Missing dimensions are re-weighted.
    parts = {"faithfulness": faith["score"], "grounding_recall": recall["score"],
             "policy_compliance": policy["score"], "rouge_l": lexical["rouge_l"]}
    used = {k: v for k, v in parts.items() if v is not None}
    wsum = sum(COMPOSITE_WEIGHTS[k] for k in used)
    composite = round(sum(COMPOSITE_WEIGHTS[k] * v for k, v in used.items()) / wsum, 3) if wsum else None

    gate_flags = [faith["pass"], recall["pass"], policy["pass"]]
    return {
        "applicable": True,
        "segment": segment,
        "searched": searched,
        "faithfulness": faith,
        "grounding_recall": recall,
        "policy_compliance": policy,
        "lexical": lexical,
        "composite_score_informational": composite,
        "passed_all_case_gates": all(f for f in gate_flags if f is not None),
        "_lexical_pair": (generated_answer, reference) if segment in LEXICAL_SEGMENTS and reference else None,
    }


# --------------------------------------------------------------------------
# Run-level aggregation: this is where the TDD gates are actually decided
# --------------------------------------------------------------------------
def _mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 4) if values else None


def aggregate_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    applicable = [r for r in results if r.get("applicable")]
    faith = [r["faithfulness"]["score"] for r in applicable]
    recall = [r["grounding_recall"]["score"] for r in applicable if r["grounding_recall"]["score"] is not None]
    pairs = [r["_lexical_pair"] for r in applicable if r.get("_lexical_pair")]
    rouge = [r["lexical"]["rouge_l"] for r in applicable if r["lexical"]["rouge_l"] is not None]
    bleu = round(corpus_bleu([p[0] for p in pairs], [p[1] for p in pairs]), 4) if pairs else None
    proxy_used = any(r["faithfulness"]["source"] == "lexical_proxy" or
                     r["grounding_recall"]["source"] == "lexical_proxy" for r in applicable)

    metrics = {
        "faithfulness_mean": _mean(faith),
        "faithfulness_strict_pct": _mean([1.0 if s >= 1.0 else 0.0 for s in faith]),
        "grounding_recall_mean": _mean(recall),
        "rouge_l_mean": _mean(rouge),
        "corpus_bleu": bleu,
        "policy_violation_cases": sum(1 for r in applicable if not r["policy_compliance"]["pass"]),
    }

    def gate(value, key):
        return None if value is None else value >= TDD_GATES[key]

    return {
        "cases_total": len(results),
        "cases_applicable": len(applicable),
        "metrics": metrics,
        "tdd_gates": {
            "faithfulness>=0.90 (nightly)": gate(metrics["faithfulness_mean"], "faithfulness"),
            "grounding_recall>=0.85 (north star)": gate(metrics["grounding_recall_mean"], "grounding_recall"),
        },
        "warning": ("Faithfulness/recall used lexical proxies for some cases; run the "
                    "Gemini 3.5 Flash judge before treating those gates as final.") if proxy_used else None,
    }


if __name__ == "__main__":
    case = {
        "segment": "S",
        "key_facts": ["Reset AirPods by placing them in the case and holding the setup button for 15 seconds."],
        "reference": {"response": {"parts": [{"text": "Try resetting your AirPods: place them in the case and hold the setup button for 15 seconds. ^AB"}]}},
    }
    answer = "You can reset your AirPods by placing them in the case and holding the setup button for 15 seconds."
    context = ["To reset AirPods: place them in their charging case, close the lid, then press and hold the setup button for 15 seconds."]

    r = evaluate_static_rubric(case, answer, context, searched=True, grounded=True, escalate=False)
    r.pop("_lexical_pair")
    print(json.dumps(r, indent=2))
    print(json.dumps(aggregate_results([evaluate_static_rubric(case, answer, context, True, True, False)]), indent=2))
