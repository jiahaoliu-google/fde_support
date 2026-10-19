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
| 6 | ROUGE-L vs. historical brand reply (TDD) | ≥ 0.35 | Search-ready, in-corpus cases only | Deterministic, after text normalization | PR |
| 7 | BLEU vs. historical brand reply (TDD) | ≥ 0.30 | Search-ready, in-corpus cases only | Deterministic, corpus-level BLEU | PR |
| 4 | Grounding recall (TDD North Star) | ≥ 85% | Search-ready, in-corpus cases | LLM judge: share of golden **key facts** present in the answer | Nightly |
| 8 | RAG faithfulness (TDD semantic QA) | ≥ 90% | Every case that searched | LLM judge (Gemini 3.5 Flash): claims supported by the retrieved records | Nightly |
| G1 | Search gating: no search on vague queries *(internal target)* | ≥ 95% | 15 vague cases + turn 1 of multi-turn cases | Deterministic: no `grounding_metadata` retrieval in that turn | PR |
| G2 | Search gating: search on specific queries *(internal target)* | ≥ 95% | Search-ready cases | Deterministic: retrieval present | PR |
| G3 | Clarifying question quality *(internal target)* | ≥ 90% | Vague turns | LLM judge with per-case rubric | Nightly |
| D1 | Retrieval hit@5 *(diagnostic, no gate)* | Report only | Search-ready, in-corpus cases | Golden `source_doc_id` found among retrieved doc IDs | — |

### Deterministic System, Latency & Cost Metrics

| # | Metric | Target / Benchmark | Evaluated on | Measurement Method | Gate / Reporting |
|---|---|---|---|---|---|
| M1 | End-to-End Latency (P50 / P90 / P95 / Max) | P95 ≤ 3.0s | All 100 cases | `time.perf_counter()` timer around agent execution per turn | PR & Nightly |
| M2 | Time-To-First-Token (TTFT) | P95 ≤ 800ms | Streaming turns | Timer from invocation start to first output event token | Nightly |
| M3 | Token Accounting (Input / Output / Cached) | Track & report totals | All 100 cases | Aggregated from ADK step `usage_metadata` (`input`, `output`, `cached`) | PR & Nightly |
| M4 | Estimated USD Cost per 1K Tickets | Target ≤ \$5.00 / 1K | All 100 cases | Exact token count × model pricing tiers (FAQ: Gemini 2.5 Pro, Router: Flash) + search query fees | PR & Nightly |
| M5 | RAG Search Overhead (P95 latency) | P95 ≤ 1.2s | Search cases | Timer around `VertexAiSearchTool` / Discovery Engine execution | Diagnostic |
| M6 | API Error / Timeout Rate | 0.0% | All 100 cases | Count of HTTP 4xx/5xx errors, rate limits (429), or timeout exceptions | PR (Gate: 0%) |
| M7 | Execution Step & Tool Call Count | Max ≤ 3 steps/turn | All turns | Deterministic audit of intermediate runner steps per turn | PR |

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
> **Human labelling is required (about 1–2 hours).** The scripts pre-label candidates with Gemini, but the TDD requires the router F1 to be computed against **human labels**. You'll review a CSV of about 140 candidates and confirm or fix: sentiment, search-readiness, the paraphrased prompt, and the key facts. The golden set is frozen only after that review.

> [!WARNING]
> **The BLEU ≥ 0.30 / ROUGE-L ≥ 0.35 targets will probably fail at first.** The references are short tweets (for example "DM us your order number"). The agent writes longer synthesized answers. The pipeline will report the real baseline and will **not** lower thresholds on its own. If the baseline is far below target, the two options are: (a) ask the agent for tweet-length answers, or (b) renegotiate the threshold in the TDD with your lead.

> [!TIP]
> **Deterministic Token & Cost Accounting Rules by Model:**
> - **Token Extraction:** ADK `Event` objects contain step-level `usage_metadata` containing `input_token_count`, `output_token_count`, and `cached_content_token_count`.
> - **Model-Specific Pricing Tiers:**
>   - **Sentiment Router (`sentiment_router_agent`)**: `gemini-2.5-flash` (\$0.075 / 1M input, \$0.30 / 1M output, \$0.01875 / 1M cached).
>   - **FAQ Assistant (`faq_assistant_agent`)**: `gemini-2.5-pro` (\$1.25 / 1M input, \$5.00 / 1M output, \$0.3125 / 1M cached).
>   - **Evaluation Judges (`eval_config.yaml`)**: `gemini-3.5-flash` (\$0.15 / 1M input, \$0.60 / 1M output).
>   - **Vertex AI Search Tool**: \$0.002 / search query execution.
> - Costs are computed per ticket segment (Urgent vs FAQ) and extrapolated to cost per 1,000 processed customer tickets.

---

## Architectural & Rule Questions

> [!IMPORTANT]
> **Q1: Rule 4 conflicts with "always grounded to RAG".** [agent.py](file:///Users/jiahaoliu/ws/fde_support/agents/faq_assistant/agent.py) tells the agent to answer from *general knowledge* when no records are found. That lowers faithfulness (#8) and contradicts your requirement. **Proposed:** when no records are found, the agent says it couldn't find a documented answer, offers escalation, and sets `grounded=false`. The 5 out-of-corpus golden cases test this.

> [!IMPORTANT]
> **Q2: How do we detect searches?** `VertexAiSearchTool` is a *model-internal* tool. It never shows up as a `function_call` in traces, so the built-in `multi_turn_tool_use_quality` and `hallucination` metrics can't see it.
> - **(A, recommended for now)** Leave the agent unchanged. The harness reads `grounding_metadata` (retrieval queries, retrieved doc IDs, snippets) from ADK events and passes the snippets to custom judges as `{context}`.
> - **(B)** Replace it with a `FunctionTool` wrapper that calls the Discovery Engine search API directly. Searches then appear as real tool calls, which unlocks the built-in tool-use and hallucination metrics and gives full control over the retrieved records. This changes the production agent and needs a redeploy.

> [!NOTE]
> **Q3: KPI definitions.**
> - **Faithfulness:** mean claim-level support score. "Strict % of cases with zero unsupported claims" is also reported.
> - **Grounding recall:** mean of (key facts present ÷ key facts expected) per case.
> - **BLEU / ROUGE normalization:** @handles, URLs, agent sign-offs (e.g. `^AB`) and emoji are removed from both sides before scoring.
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

1. **Heuristic filter.** Keep customer questions with at least 8 words. Drop replies that are only "DM us" or "please follow us", and drop replies with no actionable content.
2. **Gemini pre-labelling** (Gemini 2.5 Flash, temperature 0, JSON output). For each candidate, produce:
   - sentiment
   - answerability score
   - a paraphrased prompt, so the test doesn't just retrieve the verbatim tweet
   - 1–4 atomic key facts from the brand reply
3. **Stratified sampling.** Spread across brands such as AppleSupport, AmazonHelp, SpotifyCares and airlines, and oversample urgent candidates. Write candidate cases for review.
4. **Human review / Automated Validation.** Confirm labels, key facts, and dynamic rubrics.
5. **Freeze** to `golden_set_v1.json` (100 cases) and commit it to git. Any later change creates `v2`, so runs stay comparable.

V, M and O cases are generated from templates and actual RAG corpus questions (e.g. "how do I return?", "is shipping free?"). M cases reuse an S-type source record for turn 2.

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
      "input_tokens": 850,
      "output_tokens": 120,
      "cached_tokens": 512,
      "total_tokens": 970
    },
    "estimated_cost_usd": 0.000185,
    "search_latency_ms": 680,
    "step_count": 1,
    "status_code": 200
  }
}
```

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

Based on the core requirements and quality gates in [jliu_support_tdd.pdf](file:///Users/jiahaoliu/ws/fde_support/jliu_support_tdd.pdf), every generated answer ($A_i$) produced by the FAQ Agent is evaluated against a standardized evaluation rubric.

> [!NOTE]
> **Applicability & Segment Scope**:
> - **Segment U (Urgent / Frustrated)**: **EXCLUDED from FAQ Answer Rubric.** Routed directly to human queue (`HUMAN_QUEUE`) without invoking FAQ Agent or generating AI responses. Evaluated separately by **Router Classification Accuracy & F1 Score** (Target F1 $\ge 0.95$).
> - **Segment V (Vague, Turn 1)**: **EXCLUDED from RAG Answer Rubric.** Suppresses search (`searched == False`) and asks a clarifying question. Evaluated separately by **Search Suppression Gating G1** (Target $\ge 90\%$).
> - **Segments S & O (Neutral & Search-Ready) + Turn 2 of M**: **APPLICABLE.** Full 4-Dimension Grounded RAG Answer Rubric is strictly enforced here.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        COMPREHENSIVE STATIC EVALUATION RUBRIC                          │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ 1. Groundedness & Faithfulness (30%)     │ • Zero Uncertified Hallucinations           │
│                                           │ • Out-of-Corpus Fallback Handling          │
│                                           │ • Citation Integrity                       │
├───────────────────────────────────────────┼────────────────────────────────────────────┤
│ 2. Grounding Recall & Key Facts (25%)    │ • Key Fact Entailment (Target ≥ 85%)       │
│                                           │ • Step-by-Step Actionable Completeness     │
├───────────────────────────────────────────┼────────────────────────────────────────────┤
│ 3. Privacy, Safety & Policy (20%)        │ • PII Leakage Redaction (Model Armor)       │
│                                           │ • Brand Tone & Non-Adversarial Safety      │
│                                           │ • Prevention of Empty DM Redirects         │
├───────────────────────────────────────────┼────────────────────────────────────────────┤
│ 4. Intent & Clarification Gating (15%)   │ • Search Suppression on Vague Queries      │
│                                           │ • Single Targeted Clarifying Question      │
├───────────────────────────────────────────┼────────────────────────────────────────────┤
│ 5. Lexical & Structural Metrics (10%)     │ • ROUGE-L Score (Target ≥ 0.35)            │
│                                           │ • Corpus BLEU Score (Target ≥ 0.30)        │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### Detailed Rubric Scoring Specifications

#### Dimension 1: Groundedness & Faithfulness (Weight: 30%)
* **Target Gate**: $\ge 90\%$ Faithfulness
* **Sub-Criteria**:
  - **S1.1 Zero Hallucinations [Pass/Fail]**: The answer $A_i$ must contain zero factual claims, return windows, warranty terms, or phone numbers that are absent from the retrieved context snippets $C_i$.
  - **S1.2 Out-of-Corpus Fallback [Pass/Fail]**: When $C_i$ contains no matching records (Segment O), $A_i$ must state that no documented policy was found and offer human escalation rather than fabricating an answer.
  - **S1.3 Citation Accuracy [Pass/Fail]**: All document IDs cited in $A_i$ must strictly match source document IDs in $C_i$.

#### Dimension 2: Grounding Recall & Key Fact Coverage (Weight: 25%)
* **Target Gate**: $\ge 85\%$ Grounding Recall
* **Sub-Criteria**:
  - **S2.1 Key Fact Entailment [1–5 Scale]**: Measures the proportion of golden reference key facts ($K_i$) that are semantically entailed by $A_i$:
    $$\text{Recall Score} = \frac{\text{Count of Entailed Key Facts}}{|K_i|}$$
  - **S2.2 Step Actionability [1–5 Scale]**: Assesses whether $A_i$ provides complete actionable instructions without omitting critical prerequisites (e.g. holding case button for 15 seconds to reset AirPods).

#### Dimension 3: Privacy, Safety & Policy Compliance (Weight: 20%)
* **Target Gate**: 100% Compliance (Zero Tolerance)
* **Sub-Criteria**:
  - **S3.1 PII Redaction [Pass/Fail]**: $A_i$ must contain zero unmasked Credit Card numbers, Social Security Numbers, emails, or personal phone numbers (enforces Model Armor compliance).
  - **S3.2 Tone & Safety [Pass/Fail]**: $A_i$ must maintain a professional, empathetic support tone and avoid making unapproved legal or financial commitments.
  - **S3.3 Actionable Quality [Pass/Fail]**: Rejects low-effort unhelpful replies that merely state "DM us for help" when $C_i$ provides the answer.

#### Dimension 4: Query Intent & Clarification Gating (Weight: 15%)
* **Target Gate**: $\ge 90\%$ Gating Compliance
* **Sub-Criteria**:
  - **S4.1 Search Suppression [Pass/Fail]**: On vague single-turn queries (Segment V), `VertexAiSearchTool` execution must be suppressed (`searched == False`).
  - **S4.2 Targeted Clarification [1–5 Scale]**: When missing critical product context, $A_i$ must ask exactly 1 targeted clarifying question without assuming unstated facts.

#### Dimension 5: Lexical & Heuristic Similarity (Weight: 10%)
* **Target Gate**: ROUGE-L $\ge 0.35$, BLEU $\ge 0.30$
* **Sub-Criteria**:
  - **S5.1 ROUGE-L Score**: Evaluates longest common subsequence overlap against normalized golden reference text ($R_i$).
  - **S5.2 Corpus BLEU Score**: Evaluates 4-gram precision with brevity penalty across all test responses against golden reference corpus ($R$).

---

### Golden set tooling

- **[NEW] `scripts/golden_set.py`**:
  - `build`: filters `twcs.jsonl`, runs Gemini pre-labelling and paraphrasing, does stratified sampling and writes `tests/eval/golden/candidates_review.csv`. Templated V, M and O cases are also generated here.
  - `freeze`: validates the reviewed CSV (segment counts, every doc ID exists in `twcs.jsonl`, schema) and writes `golden_set_v1.json`.

- **[NEW] `tests/eval/golden/golden_set_v1.json`**:
  - The frozen 100-ticket golden set, under version control.

### Inference harness

- **[NEW] `tests/eval/run_inference.py`**:
  - Runs `sentiment_router_agent` and `faq_assistant_agent` **in-process** with ADK `Runner` and `InMemorySessionService`.
  - For every turn it records:
    - the parsed answer, citations and `grounded` flag
    - `searched` (whether `grounding_metadata` retrieval is present)
    - retrieval queries, retrieved doc IDs and snippets
    - **Deterministic execution metrics**: E2E turn latency, TTFT, `usage_metadata` (input, output, cached tokens), tool search latency, step count, and error/exception status.
  - Plays the scripted user turns for M cases.
  - Writes `artifacts/eval/<ts>/raw_results.jsonl`, plus `traces.json` in `EvaluationDataset` grading format with `response`, `reference`, `context`, and execution metrics filled in, ready for `agents-cli eval grade`.
  - `--target deployed` sends the same cases to the Agent Runtime endpoints for pre-release checks.

### Metrics, judges and report

- **[NEW] `tests/eval/kpi_metrics.py`**:
  - Deterministic metrics: accuracy, precision, recall, F1 and confusion matrix; text normalization plus ROUGE-L and corpus BLEU; gating compliance (G1, G2); retrieval hit@5; **system performance metrics**: P50/P90/P95/Max latency, TTFT distribution, token accounting (input/output/cached per turn and total), financial cost calculation (per case and per 1K tickets), search overhead, and API error/timeout rates.

- **[NEW] `tests/eval/eval_config.yaml`**:
  - The LLM-judge metrics above: `prompt_template`s, `rubric_group_name: case_rubrics`, judge model and sampling count.

- **[NEW] `tests/eval/report.py`**:
  - Merges the quality metrics, deterministic system performance metrics, and LLM-judge results into `kpi_report.md` / `kpi_report.json`, with a PASS/FAIL table per KPI/Metric and detailed latency/cost/token distribution tables.
  - Exits non-zero if any gate (routing accuracy/F1, deterministic gating, ROUGE/BLEU, latency P95, or 0% error rate) in the selected mode fails.

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
2. **Dependency Scan**: Run the dependency scan, then add `rouge-score` and `sacrebleu` to dev dependencies.
3. **Golden Set Build & Review**:
   - `python scripts/golden_set.py build`
   - Human review of `candidates_review.csv`
   - `python scripts/golden_set.py freeze`
4. **Baseline Execution**:
   - `bash scripts/run_eval_pipeline.sh --mode nightly`
   - Generates `kpi_report.md` and `kpi_report.json`.

