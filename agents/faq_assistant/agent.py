import os
import json
import uuid
import logging
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

try:
    from .answer_sanitizer import sanitize_answer, sanitize_response_json_text
    from .tools import get_order_status, order_id_guard, search_support_records
except ImportError:  # Loaded as a top-level module (deployed agent directory).
    from answer_sanitizer import sanitize_answer, sanitize_response_json_text
    from tools import get_order_status, order_id_guard, search_support_records

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "fde-support-sandbox-dev-jliu")
RAW_DATASTORE = os.getenv("VERTEX_AGENT_SEARCH_DATASTORE_ID", "fde-support-faq-datastore-v2")

# Format full Vertex AI Agent Search datastore resource path
if RAW_DATASTORE.startswith("projects/"):
    DATASTORE_PATH = RAW_DATASTORE
else:
    DATASTORE_PATH = f"projects/{PROJECT_ID}/locations/global/collections/default_collection/dataStores/{RAW_DATASTORE}"

# Ensure Vertex AI and ADK Telemetry environment variables for Agent Runtime
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ.setdefault("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "true")
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "SPAN_AND_EVENT"
os.environ.setdefault("OTEL_SERVICE_NAME", "fde-support-faq-assistant")
os.environ.setdefault("OTEL_RESOURCE_ATTRIBUTES", "service.name=fde-support-faq-assistant")

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

session_service = InMemorySessionService()


class FaqResponse(BaseModel):
    """Structured final response; enforced by ADK on the agent's final turn output."""
    answer: str = Field(description="Answer or clarifying question for the user.")
    citations: list[str] = Field(
        default_factory=list,
        description="doc_id values from search_support_records and/or 'Order <order_id>'; empty if no tool used.",
    )
    grounded: bool = Field(description="True only if the answer is based on tool results.")
    escalate: bool = Field(description="True if a human agent should take over.")


def sanitize_model_response(callback_context, llm_response):
    """after_model_callback: strips doc IDs / retrieval-backend references from the answer text.

    Citations keep the doc IDs for debugging; only customer-visible answer text is changed.
    Handles both native structured output (JSON text) and the set_model_response tool fallback.
    """
    content = getattr(llm_response, "content", None)
    if not content or not content.parts:
        return None
    changed = False
    for part in content.parts:
        if getattr(part, "thought", False):
            continue
        if part.text:
            cleaned = sanitize_response_json_text(part.text)
            if cleaned != part.text:
                part.text = cleaned
                changed = True
        fc = part.function_call
        if fc and fc.name == "set_model_response" and isinstance(fc.args, dict):
            answer = fc.args.get("answer")
            if isinstance(answer, str):
                cleaned = sanitize_answer(answer)
                if cleaned != answer:
                    fc.args["answer"] = cleaned
                    changed = True
    return llm_response if changed else None

# Primary FAQ Agent: grounded RAG answers + read-only order lookup, with multi-turn memory
faq_assistant_agent = Agent(
    name="faq_assistant_agent",
    model="gemini-2.5-pro",
    instruction="""
You are an official customer support assistant. You have two tools:
- get_order_status: read-only lookup of ONE specific order by numeric order ID.
- search_support_records: search of the support knowledge base (historical support Q&A records).

STEP 1 - CLASSIFY THE REQUEST:
A. ORDER-SPECIFIC: Checking the status, delivery date, tracking, or shipment/return progress of ONE specific personal order identified by a numeric order ID.
   - Alphanumeric topic hashtags are never order IDs.
   - General policy or service questions about orders, cancellations, or delays that do not ask to look up a specific numeric order ID are Type B, not Type A.
B. KNOWLEDGE / POLICY / TROUBLESHOOTING: Questions about policies, products, services, troubleshooting, travel/transit updates, billing rules, or general support topics.
C. ACTION REQUEST: Explicitly asking you to execute an account or order mutation right now (e.g. cancel an order, change an address, process a refund).
D. BOTH A and B: Combining a specific order lookup with a general policy or troubleshooting question.

STEP 2 - JUDGE CONTEXT SUFFICIENCY & SEARCH-READINESS (GATE BEFORE ANY TOOL CALL):
Evaluate the user's request across the ENTIRE conversation history (combining prior turns with the latest message) before calling any tool:

1. Order-Specific Gate (for Type A or the order part of Type D):
   - READY: The user has explicitly provided a numeric order ID in the conversation (digits only, optionally prefixed with # or ORD).
   - NOT READY: The user is asking for the status of their personal order/package without providing a numeric order ID. Do NOT call any tool. Ask the user for their order ID, and never invent or guess one.

2. Search-Readiness Gate (for Type B or the policy part of Type D):
   A query is SEARCH-READY for `search_support_records` ONLY when the cumulative conversation contains BOTH of these elements:
   - Element 1 - Subject / Entity Anchor: A specific brand, company, product, device, service, platform, or route.
   - Element 2 - Specific Intent / Topic: A concrete question, symptom, policy inquiry, or issue description.

   Apply this decision rule strictly:
   - NOT SEARCH-READY (missing Element 1 — generic question with no brand, product, or service context; OR missing Element 2 — naming a product/brand without stating the question or issue):
     Do NOT call `search_support_records` or `get_order_status`. Immediately output your final JSON response asking 1 brief, targeted clarifying question to obtain the missing context (which brand/product/service they mean, or what specific issue they are having). Set "citations": [], "grounded": false, "escalate": false.
   - SEARCH-READY (both Element 1 and Element 2 are present in the single message or combined across conversation turns):
     Proceed immediately to STEP 3 and call `search_support_records` once. Never ask a clarifying question or refuse a priori when both elements are already present.

STEP 3 - USE TOOLS (MAXIMUM 1 SEARCH CALL PER TURN):
- A (Order-Ready): call `get_order_status` exactly once with the user's numeric order ID. Do NOT call `search_support_records` for personal order status.
- B (Search-Ready): call `search_support_records` AT MOST ONCE per user turn.
  - Once `search_support_records` has returned results in the current turn, NEVER call `search_support_records` a second time—immediately synthesize your final JSON response from the returned results.
  - VERBATIM LEXICAL QUERYING: Build `query` by combining the brand/subject name with the customer's exact nouns, numbers, dates/times, locations, route/flight codes, and symptom phrases verbatim from their message. Do NOT abstract or generalize a specific customer question into a broad topic query.
  - Keep the search `query` in the SAME language as the customer's question.
  - Always leave `top_k` at its default value of 8.
- D: call `get_order_status` (if Order-Ready) for the order facts and `search_support_records` (if Search-Ready) for the policy/troubleshooting part. If either part is missing its required context, ask for the missing context.
- C: you cannot perform account or order mutations. Do not call tools for the change itself. Explain this politely and tell the user a human agent can help; set "escalate": true.

STEP 4 - ANSWER ONLY FROM TOOL RESULTS (GROUNDING & RECALL RULES):
- Every factual statement must come strictly from tool results in this conversation. Do not add facts from general knowledge.
- Order answers must report status, dates, carrier, and tracking exactly as returned. Do not round or reinterpret dates.
- `get_order_status` returns "not_found" or "invalid_order_id": say you could not find that order, ask the user to double-check the ID, and do not guess any status.
- `get_order_status` returns "order_id_not_provided": do not call it again. Ask the user for their order ID; set "grounded": false and "escalate": false.
- QUESTION-TO-QUESTION ALIGNMENT & COMPLETE FACT RECALL: Each record returned by `search_support_records` includes `brand`, `customer_question`, and `brand_response`.
  - Compare the user's message against each returned record's `brand` and `customer_question`, and always select the record whose `customer_question` most closely matches the user's exact scenario—even if other returned records sound like broader policy statements.
  - Do NOT apologize for or refuse to use a record because the user's message or a `customer_question` contains a shortened URL or link; match on the surrounding question text and answer directly from the matching `brand_response`.
  - Include EVERY distinct factual clause from that best-matching `brand_response` (the direct rule/status AND any stated exceptions, waivers, policy reasons, conditions, timelines, actionable steps, named teams, or contact details). Do NOT summarize away secondary clauses, and do NOT mix in conflicting details from unrelated records.
- UNMATCHED / OUT-OF-CORPUS REQUESTS: If `search_support_records` returns "no_results", or if none of the returned records explicitly mention the specific item, payment method, service, or topic the user asked about (even if a record mentions general eligibility categories or links), do NOT infer a yes/no answer from general policies. State that you do not have documented information on that, offer to connect the user with a human support specialist, and set "citations": [], "grounded": false, and "escalate": true.
- A tool returns "error": apologize, say the system is temporarily unavailable, set "escalate": true.
- Maintain continuity across previous conversation turns, and reply in the same language as the customer's question.

ESCALATION POLICY ("escalate" decides whether a human agent takes over the ticket):
- Set "escalate": true ONLY when: (1) the request is an action you cannot perform (type C), (2) `search_support_records` returns "no_results" or no record matches the user's specific topic/item, (3) a tool returns "error", or (4) the user explicitly asks for a human.
- In EVERY other case set "escalate": false. In particular, when asking a clarifying question (Step 2) or when `search_support_records` returns relevant results and you answer from them, set "escalate": false.
- When "escalate" is false, do NOT offer, suggest, or ask about connecting to a human agent.

STEP 5 - CUSTOMER-FACING LANGUAGE (the customer must never learn how answers are retrieved):
- Speak as the company's own support agent, directly and confidently.
- NEVER put doc_id values (e.g. twcs_123), record numbers, or source markers in the answer text. They belong ONLY in the citations array.
- NEVER mention or hint at: a datastore, knowledge base, database, search, search results, support records, documents, historical/past conversations or tickets, other users' or customers' reports, tools, or function names.
- Do NOT write attribution phrases like "Based on our records", "According to our records", "According to the knowledge base", "I found in our support history", or "Other users reported".
- When nothing relevant is found, say "I don't have specific information on that" and offer a human support specialist; do not explain why.
- Order details may be stated plainly (e.g. "Your order <order_id> is in transit...").

STRICT JSON OUTPUT FORMAT (applies to EVERY final response, including clarifying questions and after tool calls):
Your final response must be ONLY one raw JSON object: no prose before or after it, no markdown code fences.
Put doc_id references in the citations array only, never inline in the answer text.
Keys:
- answer: string (your grounded answer or 1 concise clarifying question)
- citations: array of strings (doc_id values from search_support_records and/or "Order <order_id>" for order lookups; empty [] if no tool was used or if grounded is false)
- grounded: boolean (true ONLY if the answer is based on tool results; false when asking a clarifying question or when no relevant record was found)
- escalate: boolean (true ONLY in the cases listed under ESCALATION POLICY; otherwise false)
""",
    tools=[search_support_records, get_order_status],
    before_tool_callback=order_id_guard,
    output_schema=FaqResponse,
    after_model_callback=sanitize_model_response,
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=128),
    ),
)

app = App(
    name="faq_assistant_app",
    root_agent=faq_assistant_agent
)

async def answer_faq(query_text: str, session_id: str | None = None) -> dict:
    """
    Executes the FAQ Assistant Agent using Google ADK Runner with native session history persistence.
    """
    try:
        active_session_id = session_id if session_id else f"session_faq_{uuid.uuid4().hex}"

        sess = await session_service.get_session(app_name="faq_assistant_app", user_id="user_default", session_id=active_session_id)
        if not sess:
            await session_service.create_session(app_name="faq_assistant_app", user_id="user_default", session_id=active_session_id)

        runner = Runner(
            app=app,
            session_service=session_service
        )

        msg = types.Content(parts=[types.Part.from_text(text=query_text)])
        
        answer_text = ""
        citations = []

        for event in runner.run(
            user_id="user_default",
            session_id=active_session_id,
            new_message=msg
        ):
            # Collect citations from FunctionTool responses (search doc IDs, order IDs)
            if hasattr(event, "get_function_responses"):
                for fr in event.get_function_responses() or []:
                    resp = fr.response or {}
                    if fr.name == "search_support_records":
                        for rec in resp.get("results", []) or []:
                            doc_id = rec.get("doc_id") if isinstance(rec, dict) else None
                            if doc_id:
                                citation_str = f"JSONL Entry ID: {doc_id}"
                                if citation_str not in citations:
                                    citations.append(citation_str)
                    elif fr.name == "get_order_status" and resp.get("status") == "found":
                        oid = (resp.get("order") or {}).get("order_id")
                        if oid and f"Order {oid}" not in citations:
                            citations.append(f"Order {oid}")

            # Collect grounding metadata objects from event top-level and candidates
            gms_to_process = []
            if hasattr(event, "grounding_metadata") and event.grounding_metadata:
                gms_to_process.append(event.grounding_metadata)
            if hasattr(event, "candidates") and event.candidates:
                for cand in event.candidates:
                    c_gm = getattr(cand, "grounding_metadata", None)
                    if c_gm:
                        gms_to_process.append(c_gm)

            # Process all collected grounding metadata objects
            for gm in gms_to_process:
                # Extract grounding chunks
                chunks = getattr(gm, "grounding_chunks", None) or []
                for chunk in chunks:
                    rc = getattr(chunk, "retrieved_context", None) or getattr(chunk, "web", None)
                    if rc:
                        doc_name = getattr(rc, "document_name", None)
                        title = getattr(rc, "title", None)
                        uri = getattr(rc, "uri", None)
                        
                        entry_id = None
                        if doc_name and "documents/" in doc_name:
                            entry_id = doc_name.split("documents/")[-1]
                        elif title and ("twcs" in title or "doc" in title or "JSONL" in title):
                            entry_id = title
                        elif uri and "documents/" in uri:
                            entry_id = uri.split("documents/")[-1]

                        if entry_id:
                            citation_str = f"JSONL Entry ID: {entry_id}"
                            if citation_str not in citations:
                                citations.append(citation_str)

                # Extract retrieval queries
                queries = getattr(gm, "retrieval_queries", None) or getattr(gm, "web_search_queries", None) or []
                for q in queries:
                    q_str = f"Datastore Query: '{q}'"
                    if q_str not in citations:
                        citations.append(q_str)

            # Extract generated response text
            if hasattr(event, "content") and event.content:
                if hasattr(event.content, "parts") and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            answer_text += part.text

        final_answer = answer_text
        is_grounded = True
        escalate = False
        if answer_text:
            try:
                data = json.loads(answer_text, strict=False)
                if isinstance(data, dict):
                    final_answer = data.get("answer", answer_text)
                    is_grounded = data.get("grounded", True)
                    escalate = bool(data.get("escalate", False))
                    agent_cits = data.get("citations", [])
                    for c in agent_cits:
                        if str(c) not in citations:
                            citations.append(str(c))
            except Exception:
                import re
                match = re.search(r'"answer":\s*"(.*?)"\s*,\s*"(?:citations|grounded)"', answer_text, re.DOTALL)
                if match:
                    final_answer = match.group(1).replace("\\n", "\n").replace('\\"', '"')

        return {
            "answer": sanitize_answer(final_answer) if final_answer else "I don't have specific information on that yet. Would you like me to connect you with a support specialist?",
            "citations": citations,
            "grounded": is_grounded,
            "escalate": escalate if final_answer else True,
            "session_id": active_session_id
        }
    except Exception as e:
        logger.error(f"Error executing ADK FAQ Assistant agent: {e}")
        return {
            "answer": "An error occurred while processing your request.",
            "citations": [f"Vertex AI Agent Search Datastore ({DATASTORE_PATH})"],
            "grounded": False,
            "session_id": session_id
        }
