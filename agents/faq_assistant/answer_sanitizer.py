"""Customer-facing answer sanitizer for the FAQ Assistant.

Removes internal retrieval artefacts from the text customers see:
  * knowledge-base document IDs (e.g. ``twcs_236803``) and inline citation groups
    such as ``(doc_id: twcs_1, twcs_2)`` or ``[twcs_1]``
  * Discovery Engine resource paths and internal tool names
  * phrases that reveal the retrieval backend ("based on our support records",
    "according to the knowledge base", "reports from other users", ...)

Document IDs are intentionally kept in the separate ``citations`` field for
debugging/evaluation; only the ``answer`` text is sanitized.

Stdlib-only on purpose: it is imported both by the agent (Agent Runtime) and by
the backend (Cloud Run) as a defense-in-depth layer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Knowledge-base document IDs produced by scripts/convert_twcs_to_jsonl.py.
_DOC_ID = r"twcs_\d+"

# Words that may label a citation, e.g. "(Source: twcs_1)", "[doc_id: twcs_1]".
_LABEL = (
    r"(?:doc(?:ument)?[_\s-]?ids?|docs?|documents?|sources?|records?|references?|refs?|"
    r"citations?|entry\s+ids?|ids?|see|cf\.?)"
)

# Bracketed group made only of labels, separators, and doc IDs.
_BRACKET_GROUP = re.compile(
    rf"\s*[\(\[]\s*(?:{_LABEL}\s*[:#=]?\s*)?{_DOC_ID}"
    rf"(?:\s*(?:,|;|/|&|\band\b)\s*(?:{_LABEL}\s*[:#=]?\s*)?{_DOC_ID})*\s*[\)\]]",
    re.IGNORECASE,
)

# Labelled or bare doc IDs outside brackets, e.g. "doc_id: twcs_1", "twcs_1".
_LABELLED_DOC_ID = re.compile(rf"(?:\b{_LABEL}\s*[:#=]?\s*)?\b{_DOC_ID}\b", re.IGNORECASE)

# Discovery Engine / Vertex AI Search resource paths.
_RESOURCE_PATH = re.compile(r"projects/[\w.-]+/locations/[\w.-]+/\S*?dataStores/[\w.-]+\S*", re.IGNORECASE)

# Internal names that must never reach customers.
_INTERNAL_TERMS = re.compile(
    r"\b(?:search_support_records|get_order_status|set_model_response|"
    r"vertex\s+ai(?:\s+agent)?(?:\s+search)?|discovery\s+engine|grounding\s+metadata|"
    r"cloud\s+sql|cloud\s+run|agent\s+platform|agent\s+engine|model\s+armor)\b",
    re.IGNORECASE,
)

# Terms that reveal the retrieval backend.
_BACKEND_TERMS = (
    r"(?:data\s*stores?|knowledge\s*base|support\s+records?|(?:our|the|my|internal|company)\s+records?|"
    r"historical\s+(?:support\s+)?(?:records?|conversations?|tickets?|cases?|logs?|data)|"
    r"past\s+support\s+(?:conversations?|tickets?|cases?)|"
    r"support\s+(?:logs?|tickets?|transcripts?|conversations?|history)|search\s+results?|"
    r"retrieved\s+(?:records?|documents?|results?)|documents?\s+(?:i|we)\s+(?:found|retrieved|have)|"
    r"(?:our|these)\s+(?:support\s+)?documents?|"
    r"reports?\s+from\s+other\s+(?:users|customers)|other\s+(?:users|customers)(?:'|\u2019)?\s*(?:reports?|experiences?)|"
    r"records?\s+(?:i|we)\s+found|(?:my|our)\s+search(?:es)?)"
)

# Leading attribution clause at the start of a sentence/line, e.g.
# "Based on our support records, " / "According to the knowledge base: ".
_ATTRIBUTION_CLAUSE = re.compile(
    r"(?m)(^|(?<=[.!?])\s+)"
    r"(?:based\s+on|according\s+to|from|per|in|looking\s+at|after\s+(?:searching|checking|reviewing))\s+"
    rf"[^.,:;\n]{{0,60}}?\b{_BACKEND_TERMS}\b[^.,:;\n]{{0,40}}?[,:]\s*",
    re.IGNORECASE,
)

# Residual backend nouns anywhere else are replaced with neutral wording.
_RESIDUAL_BACKEND_NOUNS = re.compile(
    r"\b(?:(?:our|the|my)\s+)?(?:support\s+)?(?:data\s*stores?|knowledge\s*base)\b", re.IGNORECASE
)

# Empty "Sources:" style lines left after ID removal.
_EMPTY_LABEL_LINE = re.compile(rf"(?im)^[ \t]*[-*\u2022]?[ \t]*{_LABEL}[ \t]*:[ \t]*$\n?")

# Brackets left holding only citation labels/punctuation, e.g. "(see )" or "[Sources: ]".
_LABEL_ONLY_BRACKET = re.compile(rf"\s*[\(\[]\s*(?:{_LABEL}\s*[:#=,;]?\s*)+[\)\]]", re.IGNORECASE)


def _strip_attribution_clauses(text: str) -> str:
    """Removes leading attribution clauses and re-capitalises the following word."""
    out, last = [], 0
    for m in _ATTRIBUTION_CLAUSE.finditer(text):
        out.append(text[last:m.start()])
        out.append(m.group(1))  # keep the sentence-boundary whitespace
        last = m.end()
        if last < len(text) and text[last].islower():
            text = text[:last] + text[last].upper() + text[last + 1:]
    out.append(text[last:])
    return "".join(out)


def _tidy(text: str) -> str:
    text = re.sub(r"[\(\[]\s*[,;]?\s*[\)\]]", "", text)        # empty () / []
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)             # space before punctuation
    text = re.sub(r"([.,;:!?])\1+", r"\1", text)                # doubled punctuation
    text = re.sub(r"[ \t]{2,}", " ", text)                      # collapse spaces, keep newlines
    text = re.sub(r"[ \t]+\n", "\n", text)                      # trailing spaces
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sanitize_answer(text: Any) -> Any:
    """Returns the customer-facing answer with internal retrieval details removed.

    Non-string input is returned unchanged.
    """
    if not isinstance(text, str) or not text:
        return text

    original = text
    text = _BRACKET_GROUP.sub("", text)
    text = _LABELLED_DOC_ID.sub("", text)
    text = _LABEL_ONLY_BRACKET.sub("", text)
    text = _RESOURCE_PATH.sub("", text)
    text = _INTERNAL_TERMS.sub("", text)
    text = _strip_attribution_clauses(text)
    text = _RESIDUAL_BACKEND_NOUNS.sub("our support resources", text)
    text = _EMPTY_LABEL_LINE.sub("", text)
    text = _tidy(text)

    if text != original:
        # Log only that a change happened (no answer content) to keep logs PII-free.
        logger.info("Sanitized customer-facing answer (removed internal retrieval references).")
    return text or original


def sanitize_response_json_text(raw: str) -> str:
    """Sanitizes the ``answer`` field of a JSON response string; citations are untouched.

    If ``raw`` is not a JSON object with an ``answer`` string, it is treated as plain text.
    """
    if not isinstance(raw, str) or not raw.strip():
        return raw
    candidate = raw.strip()
    if candidate.startswith("{") and candidate.endswith("}"):
        try:
            data = json.loads(candidate, strict=False)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("answer"), str):
            cleaned = sanitize_answer(data["answer"])
            if cleaned == data["answer"]:
                return raw
            data["answer"] = cleaned
            return json.dumps(data, ensure_ascii=False)
        return raw  # Some other JSON payload: leave as-is.
    return sanitize_answer(raw)
