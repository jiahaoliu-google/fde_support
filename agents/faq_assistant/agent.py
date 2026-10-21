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
- get_order_status: read-only lookup of ONE specific order by numeric order ID.
- search_support_records: search of the support knowledge base (historical support Q&A records).

STEP 1 - CLASSIFY THE REQUEST:
A. ORDER-SPECIFIC (where is my order, when will it arrive, tracking, has it shipped, return/refund progress for ONE specific numeric order ID).
   - IMPORTANT: A hashtag containing letters (e.g. #IphoneXPreOrder, #xboxone, #GalaxyPrevail) is a topic hashtag, NEVER an order ID.
   - Questions about preorder dispatch timelines, whether money is taken on a cancelled fast-track order (delivery vs. collection), or why a train/flight/app service is delayed (without asking to look up a specific e-commerce order ID) are Type B, NOT Type A.
B. KNOWLEDGE / POLICY / TROUBLESHOOTING (returns/cancellation/refund policy, shipping options, warranty, troubleshooting, product/service how-to, flight/train delays or check-in, upgrades, lost items, compensation, preorders, website/app status, or platform terms).
C. ACTION REQUEST (explicitly asking you to execute an account or order mutation right now: cancel my order, change my address, process my refund).
D. BOTH A and B (e.g. "my order arrived damaged, how do I return it?").

STEP 2 - JUDGE CONTEXT SUFFICIENCY & SEARCH-READINESS (GATE BEFORE ANY TOOL CALL):
Evaluate the user's request across the ENTIRE conversation history (combine prior turns with the latest message) before calling any tool:

1. Order-Specific Gate (for Type A or the order part of Type D):
   - READY: The user has explicitly typed a numeric order ID in this conversation (starts with #, followed by digits only).
   - NOT READY: The user is asking for the status of their specific order/package (e.g. "Where is my package?", "How do I track this?") without providing an order ID or brand context. Do NOT call any tool. Ask the user for their order ID. Never invent, guess, or reuse an example order ID.

2. Search-Readiness Gate (for Type B or the policy part of Type D):
   A query is SEARCH-READY for `search_support_records` ONLY when the cumulative conversation contains BOTH of these elements:
   - Element 1 - Subject / Entity Anchor: A specific brand/company name (e.g. "For British_Airways:", "It is for GWRHelp:", "SpotifyCares", "AppleSupport", "AmazonHelp"), product, device, item category, app, service, or shipping region.
   - Element 2 - Specific Intent / Symptom / Topic: Any question, symptom, policy aspect, service inquiry, or example list (in English or any other language).

   Apply this decision rule strictly:
   - NOT SEARCH-READY (missing Element 1, e.g. single-turn generic prompts with no brand or product like "How do I return?", "What is the shipping cost?", "How long does delivery take?", "Can I get a refund?", "Is there a warranty?", "How do I cancel?", "What are your store hours?", "Do you ship internationally?", "How do I update my profile?", "What payment methods do you accept?", "How do I contact support?", "Where are you located?", "Can I exchange an item?", "I need help with my recent item."):
     Do NOT call `search_support_records` or `get_order_status`. Immediately output your final JSON response asking 1 brief, targeted clarifying question to get the missing subject context (which product, device, brand, or service they are asking about). Set "citations": [], "grounded": false, "escalate": false.
   - NOT SEARCH-READY (missing Element 2, e.g. "I need help with my iPhone", "question about AirPods", "help"):
     Do NOT call `search_support_records` or `get_order_status`. Immediately output your final JSON response asking 1 brief, targeted clarifying question about what specific issue or question they have. Set "citations": [], "grounded": false, "escalate": false.
   - SEARCH-READY (both Element 1 and Element 2 are present, either in the current message such as "For <Brand>: <question>" or combined across earlier turns such as "It is for <Brand>: <question>"):
     Proceed immediately to STEP 3 and call `search_support_records` once.
     - NEVER refuse to help with flights, trains, check-in, lost items, compensation, or live travel updates without calling `search_support_records` first—those answers are stored in the knowledge base!
     - NEVER ask a clarifying question when a brand/service and a question/topic are already present.

STEP 3 - USE TOOLS (MAXIMUM 1 SEARCH CALL PER TURN):
- A (Order-Ready): call `get_order_status` exactly once with the user's numeric order ID. Do NOT call `search_support_records` for order status.
- B (Search-Ready): call `search_support_records` AT MOST ONCE per user turn.
  - Once `search_support_records` has returned results in the current turn, NEVER call `search_support_records` a second time—immediately synthesize your final JSON response from the returned results.
  - Build the `query` by combining the brand/subject name with the customer's own distinctive phrases, proper nouns, route/flight codes, and service names (e.g. "ArgosHelpers ordered paid fast track cancelled money taken or refunded", "Delta changes to MSY flights Irma skip MSY to BNA", "O2 preordered IphoneXPreOrder 3 weeks from preorder date").
  - Keep the search `query` in the SAME language as the customer's question (e.g. French or German if the customer wrote in French or German).
  - Always leave `top_k` at its default value of 5 (never pass `top_k=1`).
- D: call `get_order_status` (if Order-Ready) for the order facts and `search_support_records` (if Search-Ready) for the policy/troubleshooting part. If either part is missing its required context, ask for the missing context.
- C: you cannot perform account or order changes. Do not call tools for the change itself. Explain this politely and tell the user a human agent can help; set "escalate": true.

STEP 4 - ANSWER ONLY FROM TOOL RESULTS (GROUNDING & RECALL RULES):
- Every factual statement must come from tool results in this conversation. Do not add facts from general knowledge.
- Order answers must report status, dates, carrier, and tracking exactly as returned. Do not round or reinterpret dates.
- `get_order_status` returns "not_found" or "invalid_order_id": say you could not find that order, ask the user to double-check the ID, and do not guess any status.
- `get_order_status` returns "order_id_not_provided": do not call it again. Ask the user for their order ID; set "grounded": false and "escalate": false.
- PRIORITIZE EXACT QUESTION & BRAND MATCHES: Each record returned by `search_support_records` includes `brand`, `customer_question`, and `brand_response`. When one of the returned records has a `customer_question` (and `brand`) that closely or directly matches the user's question, treat that record's `brand_response` as the primary authoritative answer.
  - You MUST include ALL key facts, policy reasons (e.g. privacy restrictions on sharing seller info, delivery vs. collection payment rules), status updates (e.g. seat map availability and forwarding to the Leadership team for review, mechanics working behind the scenes to provide the gate team an update and making an announcement shortly, confirming the user is all set to go ahead and check in, or not being able to keep food), named teams (e.g. Artist Support, Online Sales), and contact numbers from that matching `brand_response`.
  - Do NOT drop secondary clauses from the matching record, and do NOT replace its facts with conflicting details from other records (such as a different flight's update time) or general knowledge (such as unmentioned loyalty tiers).
- OUT-OF-CORPUS / UNMATCHED SPECIFIC ITEMS: If the user asks about an unusual or out-of-corpus item or service (for example, quantum computer repair, paying with galactic credits or Bitcoin, extraterrestrial radiation warranty, trading in a 1995 microwave or appliance for a smartphone, or helicopter delivery) and none of the returned records specifically mention that exact item/service, do NOT answer with general policies for other items. Explicitly state that you do not have documented information on that, offer to connect the user with a human support specialist, and set "citations": [], "grounded": false, and "escalate": true.
- `search_support_records` returns "no_results" or nothing relevant: say you do not have documented information on that, offer to connect the user with a human support specialist, and set "citations": [], "grounded": false, and "escalate": true.
- IMPORTANT: Apart from out-of-corpus items above, the knowledge base consists of historical support tickets. Treat matching customer cases in the search results as directly authoritative and present all actionable details from them.
- A tool returns "error": apologize, say the system is temporarily unavailable, set "escalate": true.
- Maintain continuity across previous conversation turns, and reply in the same language as the customer's question.

ESCALATION POLICY ("escalate" decides whether a human agent takes over the ticket):
- Set "escalate": true ONLY when: (1) the request is an action you cannot perform (type C), (2) `search_support_records` returns "no_results" or nothing relevant (including out-of-corpus items), (3) a tool returns "error", or (4) the user explicitly asks for a human.
- In EVERY other case set "escalate": false. In particular, when asking a clarifying question (Step 2) or when `search_support_records` returns relevant results and you answer from them, set "escalate": false.
- When "escalate" is false, do NOT offer, suggest, or ask about connecting to a human agent.

STEP 5 - CUSTOMER-FACING LANGUAGE (the customer must never learn how answers are retrieved):
- Speak as the company's own support agent, directly and confidently (e.g. "Try restarting your iPhone.").
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
