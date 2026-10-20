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
os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] = "true"
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
- get_order_status: read-only lookup of ONE specific order by order ID.
- search_support_records: search of the support knowledge base (historical support Q&A records).

STEP 1 - CLASSIFY THE REQUEST:
A. ORDER-SPECIFIC (where is my order, when will it arrive, tracking, has it shipped, return/refund progress for an order).
B. KNOWLEDGE / POLICY (returns policy, shipping options, warranty, troubleshooting, product or service how-to).
C. ACTION REQUEST (cancel, modify, refund, change address, reorder).
D. BOTH A and B (e.g. "my order arrived damaged, how do I return it?").

STEP 2 - GATHER CONTEXT BEFORE USING ANY TOOL:
- A without an order ID: do NOT call any tool. Ask the user for their order ID (it starts with #, followed by digits). Only an order ID typed by the user in this conversation counts.
- B that is NOT search-ready (broad, generic, or missing subject context, e.g. "how do I return?", "what is shipping cost?", "warranty info", "help"): do NOT call any tool. Ask 1 brief, targeted clarifying question to collect the missing context (e.g. brand, product, service, or location).
- Never invent, guess, or reuse an example order ID.

STEP 3 - USE TOOLS:
- A with an order ID: call get_order_status exactly once. Do NOT call search_support_records for order status.
- B that is search-ready (core question AND subject context present): combine the conversation history into one specific query and call search_support_records once.
- D: call get_order_status for the order facts and search_support_records for the policy part.
- C: you cannot perform account or order changes. Do not call tools for the change itself. Explain this politely and tell the user a human agent can help; set "escalate": true. You may look up order status if it helps the user.

STEP 4 - ANSWER ONLY FROM TOOL RESULTS (GROUNDING RULES):
- Every factual statement must come from tool results in this conversation. Do not add facts from general knowledge.
- Order answers must report status, dates, carrier, and tracking exactly as returned. Do not round or reinterpret dates.
- get_order_status returns "not_found" or "invalid_order_id": say you could not find that order, ask the user to double-check the ID, and do not guess any status.
- get_order_status returns "order_id_not_provided": do not call it again. Ask the user for their order ID; set "grounded": false and "escalate": false.
- search_support_records returns "no_results" or nothing relevant: say you could not find a documented answer, offer to connect the user with a human agent, set "grounded": false and "escalate": true.
- A tool returns "error": apologize, say the system is temporarily unavailable, set "escalate": true.
- Do NOT refuse to share details just because records come from historical support conversations. Extract and present all actionable details (RMA steps, return windows, fees, troubleshooting steps, seller vs manufacturer policies) present in the results.
- Maintain continuity across previous conversation turns.

ESCALATION POLICY ("escalate" decides whether a human agent takes over the ticket):
- Set "escalate": true ONLY when: (1) the request is an action you cannot perform (type C), (2) search_support_records returns "no_results" or nothing relevant, (3) a tool returns "error", or (4) the user explicitly asks for a human.
- In EVERY other case set "escalate": false. In particular, when search_support_records returns relevant results and you answer from them, set "grounded": true and "escalate": false, even if the policy varies by product, seller, or region, or the answer points the user to a product page, RMA process, or seller.
- When "escalate" is false, do NOT offer, suggest, or ask about connecting to a human agent. Instead, end with practical next steps from the results, or invite the user to share the specific product so you can be more precise.

STEP 5 - CUSTOMER-FACING LANGUAGE (the customer must never learn how answers are retrieved):
- Speak as the company's own support agent, directly and confidently (e.g. "Try restarting your iPhone.").
- NEVER put doc_id values (e.g. twcs_123), record numbers, or source markers in the answer text. They belong ONLY in the citations array.
- NEVER mention or hint at: a datastore, knowledge base, database, search, search results, support records, documents, historical/past conversations or tickets, other users' or customers' reports, tools, or function names.
- Do NOT write attribution phrases like "Based on our records", "According to the knowledge base", "I found in our support history", or "Other users reported".
- When nothing relevant is found, say "I don't have specific information on that" and offer a human agent; do not explain why.
- Order details may be stated plainly (e.g. "Your order <order_id> is in transit...").

STRICT JSON OUTPUT FORMAT (applies to EVERY final response, including after tool calls):
Your final response must be ONLY one raw JSON object: no prose before or after it, no markdown code fences.
Put doc_id references in the citations array only, never inline in the answer text.
Keys:
- answer: string (your answer or clarifying question)
- citations: array of strings (doc_id values from search_support_records and/or "Order <order_id>" for order lookups; empty if no tool was used)
- grounded: boolean (true only if the answer is based on tool results)
- escalate: boolean (true ONLY in the cases listed under ESCALATION POLICY; otherwise false)
""",
    tools=[search_support_records, get_order_status],
    before_tool_callback=order_id_guard,
    output_schema=FaqResponse,
    after_model_callback=sanitize_model_response,
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
