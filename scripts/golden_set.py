#!/usr/bin/env python3
"""
Keyword-Based Golden Set Generator & Freezer for FDE Support Evaluation

Uses an expanded negative keyword lexicon to classify customer queries from `archive/twcs/twcs.jsonl`:
- 25 Segment U: Urgent / Frustrated (classified via expanded negative keywords)
- 45 Segment S: Search-Ready, In-Corpus (FAQ Agent grounded answers)
- 5 Segment O: Search-Ready, Out-of-Corpus (FAQ Agent "no documented answer")
- 15 Segment V: Vague, Single Turn (FAQ Agent clarifying questions)
- 10 Segment M: Multi-Turn Vague -> Clarified (Turn 1 clarify, Turn 2 grounded answer)
"""

import os
import json
import re
import random
from typing import List, Dict, Any

TWCS_PATH = "/Users/jiahaoliu/ws/fde_support/archive/twcs/twcs.jsonl"
OUTPUT_DIR = "/Users/jiahaoliu/ws/fde_support/tests/eval/golden"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "golden_set_v1.json")

# Comprehensive lexicon of negative, escalation, anger, billing, and failure keywords
URGENT_KEYWORDS = [
    # Severe anger & dissatisfaction
    "worst", "unacceptable", "scam", "furious", "stolen", "terrible", "disaster",
    "frustrated", "ruined", "emergency", "broken", "never again", "horrible",
    "disgusted", "outrageous", "disappointed", "pathetically", "appalling",
    "atrocious", "abysmal", "unbelievable", "ridiculous", "infuriating", "humiliated",

    # Financial & billing emergency
    "charged twice", "double charged", "overcharged", "unauthorized charge",
    "stole my money", "thief", "thieves", "refund immediately", "money back",

    # Legal & regulatory escalation
    "cancel my account", "lawsuit", "sue", "suing", "report to bbb", "ftc complaint",
    "legal action", "calling police", "fraud", "fraudulent",

    # Severe service / delivery failure
    "lost package", "damaged product", "damaged on arrival", "never arrived",
    "delayed for weeks", "no response for days", "ignored my messages",
    "bribe", "demanding money"
]


def clean_text(text: str) -> str:
    """Removes twitter handles and redundant whitespace."""
    text = re.sub(r'@[A-Za-z0-9_]+', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_key_facts(reference_text: str) -> List[str]:
    """Extracts 1-3 atomic key facts from reference response text."""
    clean = clean_text(reference_text)
    sentences = [s.strip() for s in re.split(r'[.!?]', clean) if len(s.strip()) > 10]
    if not sentences:
        sentences = [clean]
    return sentences[:3]


def load_corpus() -> List[Dict[str, Any]]:
    """Loads and filters quality records from twcs.jsonl."""
    records = []
    if not os.path.exists(TWCS_PATH):
        raise FileNotFoundError(f"Corpus file not found: {TWCS_PATH}")

    with open(TWCS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            doc_id = item.get("id")
            sdata = item.get("structData", {})
            q = sdata.get("customer_question", "")
            r = sdata.get("reference_brand_response_text", "")
            brand = sdata.get("brand_author", "Support")

            q_clean = clean_text(q)
            r_clean = clean_text(r)

            if len(q_clean.split()) >= 6 and len(r_clean.split()) >= 5:
                if re.search(r'^(please|dm us|send us a dm|pm us|follow us)\.?$', r_clean, re.I):
                    continue
                records.append({
                    "doc_id": doc_id,
                    "question": q_clean,
                    "raw_question": q,
                    "response": r_clean,
                    "raw_response": r,
                    "brand": brand
                })
    return records


def build_golden_set() -> List[Dict[str, Any]]:
    print("Loading RAG corpus from archive/twcs/twcs.jsonl...")
    corpus = load_corpus()
    print(f"Loaded {len(corpus)} valid records from corpus.")

    random.seed(42)
    random.shuffle(corpus)

    golden_cases = []

    # -------------------------------------------------------------
    # 1. Segment U: Urgent / Frustrated (25 cases via keyword lexicon)
    # -------------------------------------------------------------
    urgent_candidates = []
    normal_candidates = []

    for item in corpus:
        q_lower = item["question"].lower()
        matched_kw = [kw for kw in URGENT_KEYWORDS if kw in q_lower]
        if matched_kw:
            item["matched_keyword"] = matched_kw[0]
            urgent_candidates.append(item)
        else:
            normal_candidates.append(item)

    print(f"Found {len(urgent_candidates)} candidate tweets matching expanded negative keywords.")

    u_items = urgent_candidates[:25]
    if len(u_items) < 25:
        needed = 25 - len(u_items)
        for item in normal_candidates[:needed]:
            prefix = random.choice([
                "This is completely UNACCEPTABLE! ",
                "I am FURIOUS and demand an answer right now! ",
                "You charged me twice and this is a SCAM! ",
                "WORST SERVICE EVER! "
            ])
            item_copy = dict(item)
            item_copy["question"] = prefix + item_copy["question"]
            item_copy["matched_keyword"] = "synthesized_unacceptable"
            u_items.append(item_copy)

    for idx, item in enumerate(u_items, start=1):
        case_id = f"U-{idx:03d}"
        golden_cases.append({
            "eval_case_id": case_id,
            "segment": "U",
            "expected_sentiment": "URGENT_FRUSTRATED",
            "expected_route": "HUMAN_QUEUE",
            "search_ready": False,
            "prompt": {"role": "user", "parts": [{"text": item["question"]}]},
            "source_doc_ids": [item["doc_id"]],
            "source_customer_question": item["raw_question"],
            "reference": {"response": {"role": "model", "parts": [{"text": item["response"]}]}},
            "key_facts": ["Customer ticket requires immediate human escalation due to urgency/frustration."],
            "classification_metadata": {
                "method": "expanded_keyword_lexicon",
                "matched_keyword": item.get("matched_keyword", "urgent_lexicon")
            },
            "rubric_groups": {
                "case_rubrics": {"rubrics": [
                    {"rubric_id": "urgent_escalate", "content": {"property": {"description": "Routes directly to human escalation queue without calling FAQ RAG agent."}}}
                ]}
            }
        })

    # -------------------------------------------------------------
    # 2. Segment S: Search-Ready, In-Corpus (45 cases)
    # -------------------------------------------------------------
    s_items = normal_candidates[25:70]
    for idx, item in enumerate(s_items, start=1):
        case_id = f"S-{idx:03d}"
        key_facts = extract_key_facts(item["response"])
        golden_cases.append({
            "eval_case_id": case_id,
            "segment": "S",
            "expected_sentiment": "NEUTRAL_FACTUAL",
            "expected_route": "FAQ_AGENT",
            "search_ready": True,
            "prompt": {"role": "user", "parts": [{"text": item["question"]}]},
            "source_doc_ids": [item["doc_id"]],
            "source_customer_question": item["raw_question"],
            "reference": {"response": {"role": "model", "parts": [{"text": item["response"]}]}},
            "key_facts": key_facts,
            "rubric_groups": {
                "case_rubrics": {"rubrics": [
                    {"rubric_id": "covers_fact_1", "content": {"property": {"description": f"Answer accurately conveys: {key_facts[0]}"}}},
                    {"rubric_id": "no_hallucination", "content": {"property": {"description": "Answer stays grounded to the retrieved RAG records and does not invent unstated policies."}}}
                ]}
            }
        })

    # -------------------------------------------------------------
    # 3. Segment O: Search-Ready, Out-Of-Corpus (5 cases)
    # -------------------------------------------------------------
    out_of_corpus_prompts = [
        "Do you offer quantum computer repair services at my home?",
        "Can I pay for my order using galactic credits or Bitcoin in store?",
        "Does your warranty cover damage caused by extraterrestrial radiation?",
        "Can I trade in my 1995 microwave for a new smartphone?",
        "How do I request a helicopter delivery for my order today?"
    ]

    for idx, prompt_text in enumerate(out_of_corpus_prompts, start=1):
        case_id = f"O-{idx:03d}"
        golden_cases.append({
            "eval_case_id": case_id,
            "segment": "O",
            "expected_sentiment": "NEUTRAL_FACTUAL",
            "expected_route": "FAQ_AGENT",
            "search_ready": True,
            "prompt": {"role": "user", "parts": [{"text": prompt_text}]},
            "source_doc_ids": [],
            "source_customer_question": prompt_text,
            "reference": {"response": {"role": "model", "parts": [{"text": "I do not have documented information on that in our support records. Would you like me to connect you with a human support specialist?"}]}},
            "key_facts": ["State that no documented answer was found in support records.", "Offer human agent escalation."],
            "rubric_groups": {
                "case_rubrics": {"rubrics": [
                    {"rubric_id": "no_documented_answer", "content": {"property": {"description": "Explicitly states no documented answer was found rather than inventing an answer."}}},
                    {"rubric_id": "sets_grounded_false", "content": {"property": {"description": "Sets grounded=false and escalate=true when no records match."}}}
                ]}
            }
        })

    # -------------------------------------------------------------
    # 4. Segment V: Vague, Single-Turn (15 cases)
    # -------------------------------------------------------------
    vague_templates = [
        "How do I return?",
        "What is the shipping cost?",
        "How long does delivery take?",
        "Can I get a refund?",
        "Is there a warranty?",
        "How do I cancel?",
        "What are your store hours?",
        "Where is my package?",
        "Do you ship internationally?",
        "How do I track this?",
        "How do I update my profile?",
        "What payment methods do you accept?",
        "How do I contact support?",
        "Where are you located?",
        "Can I exchange an item?"
    ]

    for idx, vague_q in enumerate(vague_templates[:15], start=1):
        case_id = f"V-{idx:03d}"
        golden_cases.append({
            "eval_case_id": case_id,
            "segment": "V",
            "expected_sentiment": "NEUTRAL_FACTUAL",
            "expected_route": "FAQ_AGENT",
            "search_ready": False,
            "prompt": {"role": "user", "parts": [{"text": vague_q}]},
            "source_doc_ids": [],
            "source_customer_question": vague_q,
            "reference": {"response": {"role": "model", "parts": [{"text": "Could you please specify which product or order you are asking about so I can assist you?"}]}},
            "key_facts": ["Ask 1 brief clarifying question to collect missing context before searching."],
            "rubric_groups": {
                "case_rubrics": {"rubrics": [
                    {"rubric_id": "asks_clarification", "content": {"property": {"description": "Asks 1 concise clarifying question without performing an ungrounded search."}}},
                    {"rubric_id": "no_search_executed", "content": {"property": {"description": "Does not trigger search tools on vague queries."}}}
                ]}
            }
        })

    # -------------------------------------------------------------
    # 5. Segment M: Multi-Turn Vague -> Clarified (10 cases)
    # -------------------------------------------------------------
    m_items = normal_candidates[70:80]
    for idx, item in enumerate(m_items, start=1):
        case_id = f"M-{idx:03d}"
        key_facts = extract_key_facts(item["response"])
        golden_cases.append({
            "eval_case_id": case_id,
            "segment": "M",
            "expected_sentiment": "NEUTRAL_FACTUAL",
            "expected_route": "FAQ_AGENT",
            "search_ready": True,
            "agent_data": {
                "turns": [
                    {
                        "turn_index": 1,
                        "prompt": {"role": "user", "parts": [{"text": "I need help with my recent item."}]},
                        "expected_search_ready": False,
                        "expected_behavior": "ask_clarifying_question"
                    },
                    {
                        "turn_index": 2,
                        "prompt": {"role": "user", "parts": [{"text": f"It is for {item['brand']}: {item['question']}"}]},
                        "expected_search_ready": True,
                        "expected_behavior": "search_and_answer"
                    }
                ]
            },
            "source_doc_ids": [item["doc_id"]],
            "source_customer_question": item["raw_question"],
            "reference": {"response": {"role": "model", "parts": [{"text": item["response"]}]}},
            "key_facts": key_facts,
            "rubric_groups": {
                "case_rubrics": {"rubrics": [
                    {"rubric_id": "multi_turn_continuity", "content": {"property": {"description": "Clarifies in turn 1 then provides grounded answer in turn 2 using full context."}}}
                ]}
            }
        })

    return golden_cases


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    golden_set = build_golden_set()

    print(f"\nWriting 100 frozen golden set records to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(golden_set, f, indent=2)

    print(f"Successfully generated golden set with {len(golden_set)} records!")

    counts = {}
    for case in golden_set:
        seg = case["segment"]
        counts[seg] = counts.get(seg, 0) + 1

    print("\nGolden Set Segment Distribution:")
    for seg, count in sorted(counts.items()):
        print(f"  Segment {seg}: {count} cases")


if __name__ == "__main__":
    main()
