# KPI-Driven Evaluation Pipeline: Golden Set, Dynamic Rubrics & CI Gates

## Goal

Build one evaluation pipeline that measures every **AI-quality KPI** and **Deterministic System Metric (latency, cost, token accounting, error rate)** in [jliu_support_scoping.pdf](file:///Users/jiahaoliu/ws/fde_support/jliu_support_scoping.pdf) and [jliu_support_tdd.pdf](file:///Users/jiahaoliu/ws/fde_support/jliu_support_tdd.pdf), plus the new RAG search-gating behavior. It uses a **100-ticket golden set taken from the records indexed in the RAG datastore**, so every expected FAQ answer traces back to a real document ID.

This replaces the previous FAQ-only plan. Sentiment router evaluation, deterministic system efficiency metrics (latency, token, cost accounting), and the TDD's PR/nightly gate split are now included.

---

## Evaluation Architecture: Offline vs. Online

The evaluation strategy explicitly separates **Offline Evaluation** (blocking pre-deployment verification in CI) from **Online Evaluation** (continuous production telemetry & quality monitoring).

| Dimension | Offline Evaluation (Pre-Deployment / CI) | Online Evaluation (Post-Deployment / Telemetry) |
|---|---|---|
| **Trigger / Cadence** | On every PR submit and Nightly CI scheduled build | Continuous production telemetry stream + periodic sampling jobs |
| **Data Source** | Versioned 100-ticket golden set (`golden_set_v1.json`) | Live production customer sessions from Cloud Logging / BigQuery |
| **Execution Environment** | In-process ADK `Runner` harness / staging endpoints | Production Cloud Run / Agent Runtime serving infrastructure |
| **Quality Measurement** | Exact ground-truth metrics: Router F1, ROUGE-L, BLEU, Key Fact Recall, Gating compliance | Asynchronous LLM judge on sampled live traces, human escalation rate, CSAT / user feedback |
| **Deterministic Metrics** | Controlled baseline P95 latency (M1), TTFT (M2), projected token usage (M3), cost modeling (M4) | Live P95/P99 latency (Cloud Trace), actual token consumption & Cloud Billing, 4xx/5xx/rate limit counts (M6) |
| **Output / Artifacts** | `kpi_report.md` / `kpi_report.json` with PR gate blocking | BigQuery Agent Analytics, Cloud Trace dashboards, and PagerDuty alert triggers |

---

## KPIs & Deterministic System Metrics Covered

### AI Quality & Routing KPIs

| # | KPI (source) | Target | Evaluated on | Method | Gate |
|---|---|---|---|---|---|
| 1 | Routing accuracy (TDD North Star) | ≥ 95% | All 100 | Deterministic: predicted vs. labelled sentiment | PR |
| 5 | Urgent/Frustrated **F1** (TDD CI gate) | ≥ 0.95 | All 100 | Deterministic: precision / recall / F1 + confusion matrix | PR |
| 9 | Urgent tickets bypass RAG to the human queue (Scoping) | 100% | 25 urgent cases | pytest on backend routing logic (`routed_to`, FAQ agent never called) | PR |
| 6 | ROUGE-L vs. historical brand reply (TDD) | ≥ 0.35 | S cases + turn 2 of M (cases with a real historical reply) | Deterministic, after text normalization; mean of per-case LCS F1 | PR |
| 7 | BLEU vs. historical brand reply (TDD) | ≥ 0.30 | S cases + turn 2 of M | Deterministic, corpus-level BLEU-4 with brevity penalty | PR |
| 4 | Grounding recall (TDD North Star) | ≥ 85% | S cases + turn 2 of M | LLM judge: share of golden **key facts** present in the answer | Nightly |
| 8 | RAG faithfulness (TDD semantic QA) | ≥ 90% | Every case that searched | LLM judge (Gemini 3.5 Flash, per TDD): no hallucinations **and** no agent policy violations | Nightly |
| G1 | Search gating: no search on vague queries *(internal target)* | ≥ 95% | 15 vague cases + turn 1 of multi-turn cases | Deterministic: no `search_support_records` `function_call` in that turn | PR |
| G2 | Search gating: search on specific queries *(internal target)* | ≥ 95% | Search-ready cases | Deterministic: `search_support_records` `function_call` present | PR |
| G3 | Clarifying question quality *(internal target)* | ≥ 90% | Vague turns | LLM judge with per-case rubric | Nightly |
| D1 | Retrieval hit@5 *(diagnostic, no gate)* | Report only | S cases | Golden `source_doc_id` found among `doc_id`s in the `search_support_records` `function_response` | — |

### Deterministic System, Latency & Cost Metrics

The TDD asks to track model cost and latency overhead ("Operational & Business Intelligence") but sets **no numeric targets** for them. Targets below marked *(proposed)* are project choices; please confirm or change them.

| # | Metric | Target / Benchmark | Evaluated on | Measurement Method | Gate / Reporting |
|---|---|---|---|---|---|
| M1 | End-to-End Latency (P50 / P90 / P95 / Max) | P95 ≤ 3.0s *(proposed)* | All 100 cases | Timer around agent execution per turn | PR & Nightly |
| M2 | Time-To-First-Token (TTFT) | P95 ≤ 800ms *(proposed)* | Streaming turns | Timer from invocation start to first output event token | Nightly |
| M3 | Token Accounting (Input / Output / Thinking / Cached) | Track & report totals | All 100 cases | Aggregated from Gemini `usage_metadata` (`prompt_token_count`, `candidates_token_count`, `thoughts_token_count`, `cached_content_token_count`) | PR & Nightly |
| M4 | Estimated USD Cost per 1K Tickets | Report only (no TDD target) | All 100 cases | Token counts × rates in [pricing.yaml](file:///Users/jiahaoliu/ws/fde_support/tests/eval/pricing.yaml) + search query fees | PR & Nightly |
| M5 | RAG Search Overhead (P95 latency) | P95 ≤ 1.2s *(proposed)* | Search cases | Duration of the `search_support_records` tool call (Discovery Engine) | Diagnostic |
| M6 | API Error / Timeout Rate | 0.0% *(proposed)* | All 100 cases | Count of HTTP 4xx/5xx errors, rate limits (429), or timeout exceptions | PR (Gate: 0%) |
| M7 | Execution Step & Tool Call Count | Max ≤ 3 steps/turn *(proposed)* | All turns | Deterministic audit of intermediate runner steps per turn | PR |

> [!NOTE]
> Infrastructure KPIs are covered elsewhere and are out of scope here:
> - #2 circuit breaker sensitivity: existing [test_circuit_breaker_behavior.py](file:///Users/jiahaoliu/ws/fde_support/tests/test_circuit_breaker_behavior.py)
> - #3 p95 DB write latency ≤ 100 ms and #10 < 500 ms fallback overhead: Cloud Trace
> - #11 ≥ 80% coverage: pytest-cov
>
> The pipeline script will optionally run the coverage gate so one command reports every gate.

---

## Key Requirements & Considerations

> [!IMPORTANT]
> **Human review of labels is required (about 1 hour).** The TDD computes router F1 against **human labels**. The golden set is pre-labelled by keyword matching, which has known misses. Examples from the current set: `U-009` ("worst score yet"), `U-011` ("worst UI's") and `U-021` ("broken down train") may not be urgent. Review the 25 U cases and a sample of S cases before freezing.

> [!WARNING]
> **The BLEU ≥ 0.30 / ROUGE-L ≥ 0.35 targets will probably fail at first.** The references are short tweets (for example "DM us your order number"). The agent writes longer synthesized answers. The pipeline will report the real baseline and will **not** lower thresholds on its own. If the baseline is far below target, the two options are: (a) ask the agent for tweet-length answers, or (b) renegotiate the threshold in the TDD with your lead.

> [!TIP]
> **Token & Cost Accounting.** All rates live in [pricing.yaml](file:///Users/jiahaoliu/ws/fde_support/tests/eval/pricing.yaml) (checked 2026-10-19):
> - **Sentiment Router**: `gemini-2.5-flash`, \$0.30 / 1M input, \$2.50 / 1M output, \$0.03 / 1M cached.
> - **FAQ Assistant**: `gemini-2.5-pro`, \$1.25 / 1M input, \$10.00 / 1M output, \$0.125 / 1M cached (≤200K-token prompts).
> - **LLM judge**: `gemini-3.5-flash`, rate **not yet verified** (left `null`).
> - **Search** (`search_support_records` → Discovery Engine): \$1.50 / 1K queries (Standard) or \$4.00 / 1K (Enterprise). The datastore edition still needs confirming.
> - **Formula:** `prompt_token_count` already *includes* cached tokens, and thinking tokens are billed as output:
>   `cost = (prompt − cached)·in + cached·cache + (candidates + thoughts)·out`.
> - Costs are reported per segment (urgent tickets only hit the router) and scaled to cost per 1,000 tickets.

---

## Architectural & Rule Questions

> [!NOTE]
> **Q1 (resolved): out-of-corpus behaviour.** [agent.py](file:///Users/jiahaoliu/ws/fde_support/agents/faq_assistant/agent.py#L115-L120) now says: when `search_support_records` returns nothing relevant, say no documented answer was found, offer a human agent, and set `grounded=false` and `escalate=true`. The 5 out-of-corpus (O) golden cases test this.

> [!NOTE]
> **Q2 (resolved): how searches are detected.** `search_support_records` is a regular ADK `FunctionTool` that calls the Discovery Engine API directly ([tools.py](file:///Users/jiahaoliu/ws/fde_support/agents/faq_assistant/tools.py#L196)). Every search therefore appears in traces as a `function_call` (the query) plus a `function_response` (the `doc_id`s and snippets). Consequences:
> - Gating (G1/G2), hit@5 (D1) and citation checks read the trace directly; no `grounding_metadata` parsing is needed.
> - The built-in Agent Platform metrics `hallucination` and `multi_turn_tool_use_quality` can see the searches and are usable.

> [!NOTE]
> **Q3: KPI definitions.**
> - **Faithfulness:** mean per-case score from the Gemini 3.5 Flash judge. A case scores 0 if it contains an unsupported claim **or** breaks an agent policy (doc IDs in the answer text, disclosing the retrieval mechanism, a bare "DM us" reply). "Strict % of cases with zero unsupported claims" is also reported.
> - **Grounding recall:** mean of (key facts present ÷ key facts expected) per case.
> - **BLEU / ROUGE normalization:** HTML entities are decoded; @handles, URLs and agent sign-offs (e.g. `^AB`) are removed; emoji and punctuation are dropped by the tokenizer. Implemented in [static_rubric_evaluator.py](file:///Users/jiahaoliu/ws/fde_support/tests/eval/static_rubric_evaluator.py).
> - **Latency & Throughput:** E2E Latency is measured per turn in milliseconds (`ms`). TTFT is captured when streaming response chunks.

---

## Golden Set Design (100 tickets, one unified file)

### Composition

| Segment | Count | Expected sentiment | Expected route | Purpose / KPIs |
|---|---:|---|---|---|
| **U: Urgent / frustrated** | 25 | `URGENT_FRUSTRATED` | Human queue | #1, #5, #9, M1, M3, M4 |
| **S: Search-ready, in-corpus** | 45 | `NEUTRAL_FACTUAL` | FAQ agent, search | #4, #6, #7, #8, G2, D1, M1, M3, M4, M5 |
| **O: Search-ready, out-of-corpus** | 5 | `NEUTRAL_FACTUAL` | FAQ agent, search, "no documented answer" | #8, Q1 behavior, M1, M3, M4 |
| **V: Vague, single turn** | 15 | `NEUTRAL_FACTUAL` | FAQ agent, **no search**, clarifying question | G1, G3, M1, M3, M4 |
| **M: Multi-turn (vague → clarified)** | 10 | `NEUTRAL_FACTUAL` | Turn 1: no search; turn 2: search | G1, G2, G3, #4, #8, M1, M3, M4, M7 |
| **Total Golden Set** | **100** | | | |

With 25 positive urgent cases, F1 ≥ 0.95 leaves room for only 1–2 router mistakes. That is enough to make the gate meaningful.

### How records are sourced from the RAG corpus

The local file [archive/twcs/twcs.jsonl](file:///Users/jiahaoliu/ws/fde_support/archive/twcs/twcs.jsonl) contains the exact 100,000 records indexed in `fde-support-faq-datastore-v2`. Each record has the fields `id`, `customer_question` and `reference_brand_response_text`.

Implemented in [scripts/golden_set.py](file:///Users/jiahaoliu/ws/fde_support/scripts/golden_set.py) (deterministic, `random.seed(42)`):

1. **Heuristic filter.** Decode HTML entities and strip @handles and URLs. Keep customer questions with ≥ 6 words and replies with ≥ 5 words. Drop replies that are only "DM us" / "follow us". This leaves 86,676 records.
2. **Keyword pre-labelling.** A question is `URGENT_FRUSTRATED` if it contains any phrase from the urgent lexicon as a **whole word or phrase** (regex `\b…\b`). The lexicon includes the TDD's own examples ("very upset", "late shipping", "charge error"). 4,982 records match.
3. **Sampling.** Shuffle, take the first 25 urgent matches for U, and take non-matching records for S (45) and M (10).
4. **Key facts.** Up to the first 3 sentences of the historical brand reply.
5. **Human review**, then **freeze** to `golden_set_v1.json` and commit it. Any later change creates `v2`, so runs stay comparable.

V and O cases come from fixed templates. M cases reuse an S-type source record for turn 2.

> [!WARNING]
> **Known limitation:** S prompts are the cleaned **verbatim** customer tweets, and those exact tweets are in the search index. Retrieval is therefore easier than in production, which inflates hit@5 and probably grounding recall. Paraphrasing the S prompts (by Gemini or by hand) would fix this; it is not done yet.

### Record schema (ADK `EvaluationDataset`-compatible, with extra fields)

```json
{
  "eval_case_id": "S-017",
  "segment": "S",
  "expected_sentiment": "NEUTRAL_FACTUAL",
  "expected_route": "FAQ_AGENT",
  "search_ready": true,
  "prompt": {"role": "user", "parts": [{"text": "My AirPods keep disconnecting from my iPhone after the iOS 11 update, how do I fix it?"}]},
  "source_doc_ids": ["twcs_119237"],
  "source_customer_question": "@AppleSupport airpods keep dropping since updating to 11.1 ...",
  "reference": {"response": {"role": "model", "parts": [{"text": "Try resetting your AirPods: place them in the case, hold the setup button for 15 seconds ..."}]}},
  "key_facts": [
    "Reset AirPods by holding the setup button on the case for about 15 seconds",
    "Re-pair the AirPods with the iPhone after resetting"
  ],
  "rubric_groups": {
    "case_rubrics": {"rubrics": [
      {"rubric_id": "covers_reset", "content": {"property": {"description": "Answer explains how to reset the AirPods via the case setup button."}}},
      {"rubric_id": "no_invented_policy", "content": {"property": {"description": "Answer does not invent warranty or replacement terms absent from retrieved records."}}}
    ]}
  },
  "execution_metrics": {
    "latency_ms": 1420,
    "ttft_ms": 350,
    "token_usage": {
      "prompt_token_count": 850,
      "cached_content_token_count": 512,
      "candidates_token_count": 120,
      "thoughts_token_count": 0,
      "total_token_count": 970
    },
    "estimated_cost_usd": 0.003187,
    "search_latency_ms": 680,
    "step_count": 1,
    "status_code": 200
  }
}
```

The sample cost uses Gemini 2.5 Pro plus one Standard search: (338 × \$1.25 + 512 × \$0.125 + 120 × \$10.00) / 1M + \$0.0015 = \$0.003187.

M cases use `agent_data.turns` with scripted user turns instead of `prompt`.

---

## Dynamic Rubrics & LLM Evaluator Architecture

Evaluation of qualitative generated answers uses **Gemini 3.5 Flash** as an automated LLM Judge (`temperature: 0`, 3 independent samples per case with majority voting / score averaging). 

Rather than relying solely on static generic metrics, the evaluator applies **Dynamic Per-Case Rubrics** defined inside each golden set ticket's `rubric_groups`.

### Evaluator Execution Flow

```
  ┌─────────────────────────────┐      ┌──────────────────────────────┐
  │   Agent Generated Answer    │      │  Golden Ticket Context       │
  │    (from run_inference)     │      │  • key_facts                 │
  └──────────────┬──────────────┘      │  • case_rubrics              │
                 │                     │  • retrieved context snippets │
                 │                     └──────────────┬───────────────┘
                 │                                    │
                 └──────────────────┬─────────────────┘
                                    ▼
                 ┌────────────────────────────────────┐
                 │    LLM Evaluator Prompt Builder    │
                 │  (Injects response, context &      │
                 │   dynamic case-specific rubrics)   │
                 └──────────────────┬─────────────────┘
                                    ▼
                 ┌────────────────────────────────────┐
                 │     LLM Judge: Gemini 3.5 Flash    │
                 │     (3 samples @ temperature=0)    │
                 └──────────────────┬─────────────────┘
                                    ▼
                 ┌────────────────────────────────────┐
                 │ Structured Evaluation Output JSON  │
                 │ • claims_supported_ratio           │
                 │ • key_facts_recalled_ratio         │
                 │ • rubric_items_passed / total      │
                 └────────────────────────────────────┘
```

### Dynamic Rubric Specifications

| Metric | Prompt Inputs | Evaluator Scoring Logic | Target |
|---|---|---|---|
| `case_rubric_adherence` | `{response}`, per-case `rubric_groups.case_rubrics` | Evaluates each dynamic rubric property defined for that specific ticket (e.g. "covers reset steps", "no invented warranty terms"). Score = $\frac{\text{Passed Rubrics}}{\text{Total Case Rubrics}}$ | ≥ 90% |
| `rag_faithfulness` | `{response}`, `{context}` (retrieved record snippets) | Atomizes response into individual factual claims. Checks if every claim is directly entailed by `{context}`. Score = $\frac{\text{Supported Claims}}{\text{Total Claims}}$ | ≥ 90% |
| `grounding_key_fact_recall` | `{response}`, `key_facts` | Checks if each atomic reference key fact is present in `{response}`. Score = $\frac{\text{Key Facts Present}}{\text{Total Key Facts}}$ | ≥ 85% |
| `clarification_quality` | `{response}`, `{prompt}`, `rubric_groups` | For vague turns, checks if answer asks exactly 1 targeted clarifying question without assuming unstated facts or providing premature answers. Binary 1/0. | ≥ 90% |
| `search_gating_compliance` | `{prompt}`, `{agent_data}`, `searched` flag | Verifies if the agent correctly triggered search for specific queries or suppressed search for vague queries. Binary 1/0. | ≥ 95% |
| `final_response_quality` | `{response}`, `{prompt}` | Built-in holistic assessment score from 1.0 to 5.0 rating tone, helpfulness, and conciseness. | Avg ≥ 4.0 |

---

## Comprehensive Static Evaluation Rubric (TDD-Aligned)

Every FAQ Agent answer ($A_i$) in scope is scored against the rubric below. Implementation: [static_rubric_evaluator.py](file:///Users/jiahaoliu/ws/fde_support/tests/eval/static_rubric_evaluator.py), with 18 hand-computed unit tests in [test_static_rubric_evaluator.py](file:///Users/jiahaoliu/ws/fde_support/tests/eval/test_static_rubric_evaluator.py).

**Where each rule comes from:**

| Element | Source |
|---|---|
| Faithfulness ≥ 90%, judged by Gemini 3.5 Flash, covering "hallucinations or agent policy violations" | TDD § Semantic QA Evaluation |
| Grounding recall ≥ 85% | TDD North Star metrics |
| ROUGE-L ≥ 0.35 and BLEU ≥ 0.30 vs. the historical agent reply | TDD § Statistical & Heuristic QA (PR gate) |
| Policy checks (no doc IDs in the answer, no disclosure of the retrieval mechanism, no bare "DM us") | FAQ agent instructions ([agent.py](file:///Users/jiahaoliu/ws/fde_support/agents/faq_assistant/agent.py#L115-L135)), counted as "agent policy violations" |
| Output PII check | **Project addition.** The TDD only requires Model Armor to redact *inputs*. |
| Dimension weights (35/30/25/10) | **Project choice.** Used only for an informational composite; the pass/fail decision comes from the TDD gates. |

> [!NOTE]
> **Applicability & Segment Scope**:
> - **Segment U (Urgent / Frustrated)**: **EXCLUDED.** Routed straight to the human queue; no AI answer is generated. Scored by **router F1** (target ≥ 0.95).
> - **Segment V (Vague, single turn)**: **EXCLUDED.** No search; the agent asks a clarifying question. Scored by **search gating G1** (target ≥ 95%) and G3.
> - **Segments S, O and turn 2 of M**: **APPLICABLE.** BLEU/ROUGE-L use only S and M, because O references are synthetic.

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│            STATIC EVALUATION RUBRIC (RAG RESPONSES: S, O, M turn 2)                    │
├───────────────────────────────────────────┬────────────────────────────────────────────┤
│ 1. Faithfulness (TDD ≥ 90%)               │ • No unsupported claims (LLM judge)        │
│                                           │ • Out-of-corpus fallback (O cases)         │
│                                           │ • Any policy violation → case scores 0     │
├───────────────────────────────────────────┼────────────────────────────────────────────┤
│ 2. Grounding Recall (TDD ≥ 85%)           │ • Key facts entailed by the answer         │
├───────────────────────────────────────────┼────────────────────────────────────────────┤
│ 3. Policy Compliance (zero tolerance)     │ • Agent-instruction rules (see table)      │
│                                           │ • Output PII (project addition)            │
├───────────────────────────────────────────┼────────────────────────────────────────────┤
│ 4. Lexical (TDD PR gate)                  │ • Mean ROUGE-L ≥ 0.35                      │
│                                           │ • Corpus BLEU-4 ≥ 0.30                     │
└───────────────────────────────────────────┴────────────────────────────────────────────┘
```

### Detailed Rubric Scoring Specifications

#### Dimension 1: Faithfulness
* **Gate (TDD)**: mean ≥ 0.90 across applicable cases.
* **S1.1 No unsupported claims**: every factual claim (steps, windows, fees, phone numbers) must be supported by the `search_support_records` / `get_order_status` responses in that turn. Scored by the Gemini 3.5 Flash judge and passed in as `judge_scores["faithfulness"]`.
* **S1.2 Out-of-corpus fallback (O)**: rule-based. The answer must say no documented answer exists, and the agent must return `grounded=false` and `escalate=true`.
* **S1.3 Policy override**: any Dimension 3 violation sets the case score to 0.
* Citation integrity (cited `doc_id`s must appear in the tool response) is checked by a separate code metric on the platform; see the implementation plan.

#### Dimension 2: Grounding Recall
* **Gate (TDD)**: mean ≥ 0.85 over S and M cases.
* $\text{Recall}_i = \frac{\text{entailed key facts}}{|K_i|}$, judged by the LLM judge and passed in as `judge_scores["grounding_recall"]`.

#### Dimension 3: Policy Compliance
* **Gate**: zero violations (deterministic regex checks).
* `doc_id_in_answer`, `retrieval_disclosure` and `lazy_dm_redirect` come from the agent's instructions. `pii_email`, `pii_ssn` and `pii_card` are the project-added output checks.

#### Dimension 4: Lexical
* **Gate (TDD)**: mean ROUGE-L ≥ 0.35 and **corpus** BLEU-4 ≥ 0.30, over S and M cases.
* ROUGE-L is the LCS F1. BLEU uses pooled n-gram counts with a brevity penalty (Papineni 2002). Per-case smoothed BLEU is reported as a diagnostic only.

> [!WARNING]
> **Without judge scores, the evaluator falls back to lexical proxies** for Dimensions 1–2 and adds a `warning` to the aggregate. Proxy results must not be reported as the TDD faithfulness or recall gate.

---

### Golden set tooling

- **[DONE] [scripts/golden_set.py](file:///Users/jiahaoliu/ws/fde_support/scripts/golden_set.py)**:
  - One command, `python3 scripts/golden_set.py`, with seed 42. It loads `twcs.jsonl`, filters, applies whole-word keyword labels, samples by segment and writes `golden_set_v1.json` directly.
  - Gemini pre-labelling and paraphrasing are **not** implemented (see the known limitation above).
  - *(Planned)* A `freeze` check that every `source_doc_id` exists in `twcs.jsonl` and segment counts match.

- **[NEW] `tests/eval/golden/golden_set_v1.json`**:
  - The frozen 100-ticket golden set, under version control.

- **[DONE] [build_eval_datasets.py](file:///Users/jiahaoliu/ws/fde_support/scripts/build_eval_datasets.py)**: the **only** path into the rubric judge. It keeps a case only if `expected_sentiment == NEUTRAL_FACTUAL` **and** `search_ready == true` (60 cases: S 45, O 5, M 10). It exits with an error if any U or V case gets through.
- **[DONE] [eval_config_faq_rubric.yaml](file:///Users/jiahaoliu/ws/fde_support/tests/eval/eval_config_faq_rubric.yaml)**: `rubric_scope_check` (second guard), the built-in `hallucination` metric, and three Gemini 3.5 Flash judges: `rag_faithfulness`, `grounding_key_fact_recall` (reads `{key_facts}`) and `case_rubric_adherence` (reads `{rubric_items}`). The judges read evidence from `{agent_data}` (the `search_support_records` results). Platform constraints found in the smoke run:
  - `judge_model` must be a full resource name: `projects/<p>/locations/global/publishers/google/models/gemini-3.5-flash`.
  - Each case may have only **one** rubric group; built-in metrics fail otherwise.
  - Templates resolve **top-level** case fields only. `{rubrics}` and dotted paths are not filled in, so the builder adds a flat `rubric_items` list.

### Inference harness

- **[DONE] [run_inference.py](file:///Users/jiahaoliu/ws/fde_support/tests/eval/run_inference.py)**:
  - Runs `sentiment_router_agent` and `faq_assistant_agent` **in-process** with ADK `Runner` and `InMemorySessionService` (pre-deployment), against real Vertex AI Gemini and the Discovery Engine datastore.
  - Mirrors `/api/chat` routing per message: fresh sentiment session, FAQ only when `NEUTRAL_FACTUAL`, fail closed on URGENT / invalid / errored classifications. Plays the scripted user turns for M cases on one FAQ session.
  - For every turn it records the sentiment label, parsed FAQ answer (`grounded`, `escalate`), search queries, `doc_id`s and tool status, plus **deterministic execution metrics**: turn latency, per-agent latency, search tool latency, `usage_metadata` (prompt, cached, candidates, thoughts), LLM call count, cost, and error type (timeout / rate_limited / server_error / client_error).
  - Writes `artifacts/eval/<ts>/raw_results.jsonl` and `system_metrics.json`. `--suite router` runs only the sentiment agent; `--enforce-gates` exits 1 on a failed gate.
  - *(Planned)* TTFT (needs streaming), `traces.json` in grading format, and `--target deployed`.

### Metrics, judges and report

- **[DONE] [kpi_metrics.py](file:///Users/jiahaoliu/ws/fde_support/tests/eval/kpi_metrics.py)**: accuracy, per-class precision / recall / F1 and confusion matrix (F1 is `None`, not 0 or 1, when undefined); P50/P90/P95/max latency; token accounting; cost from `pricing.yaml` (an unknown price gives `None`, never \$0). ROUGE-L and BLEU stay in `static_rubric_evaluator.py`. *(Planned)* G1/G2 gating compliance and hit@5.

- **[NEW] `tests/eval/eval_config.yaml`**:
  - The LLM-judge metrics above: `prompt_template`s, `rubric_group_name: case_rubrics`, judge model and sampling count.

- **[DONE] [generate_kpi_report.py](file:///Users/jiahaoliu/ws/fde_support/scripts/generate_kpi_report.py)** (instead of `tests/eval/report.py`):
  - Merges `system_metrics.json`, the trace file (ROUGE-L / BLEU) and the grade file (LLM judges) into `kpi_report.md` / `kpi_report.json`. **No value is hard-coded:** a KPI without a source artifact shows NOT MEASURED. Each row shows its source and sample size; findings are generated from the data.
  - TDD thresholds come from `static_rubric_evaluator.TDD_GATES` (grounding recall 0.85).
  - `--enforce-gates` exits non-zero if any measured gate fails.

### Routing gate

- **[NEW] `tests/test_routing_bypass.py`**:
  - Parametrized over the 25 urgent golden messages with the sentiment agent mocked to return `URGENT_FRUSTRATED`.
  - Asserts `routed_to == "Human Escalation Queue"`, ticket status `ESCALATED`, and that `call_faq_agent` is **never awaited** (KPI #9).

### Orchestration

- **[NEW] `scripts/run_eval_pipeline.sh`**:
  - `--mode pr`: inference, then deterministic gates (#1, #5, #6, #7, G1, G2, M1, M3, M4, M6), then `pytest tests/test_routing_bypass.py`, then the report. Optional `--with-coverage` runs the ≥ 80% gate.
  - `--mode nightly`: everything in PR mode plus `agents-cli eval grade --config tests/eval/eval_config.yaml` (#4, #8, G3, rubrics, M2 TTFT) and `agents-cli eval analyze` on failures.
  - `--compare <prev_report.json>` diffs per-KPI quality and system performance/cost metrics against a previous run.

---

## Execution Workflow

1. **Prerequisite**: run `gcloud auth application-default login`.
2. **Dependencies**: none new. ROUGE-L and BLEU are implemented in pure Python, so `rouge-score` and `sacrebleu` aren't needed.
3. **Golden Set Build & Review**:
   - `python3 scripts/golden_set.py` regenerates `tests/eval/golden/golden_set_v1.json` deterministically.
   - Human review of the 25 U cases (and a sample of S) in that file. Fix labels by hand, then commit.
   - `python3 -m unittest tests/eval/test_static_rubric_evaluator.py` checks the metrics and the keyword matcher.
4. **Baseline Execution**:
   - `bash scripts/run_eval_pipeline.sh --mode nightly`
   - Generates `kpi_report.md` and `kpi_report.json`.

