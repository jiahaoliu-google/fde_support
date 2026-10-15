import os
import json
import uuid
import logging
import asyncio
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import VertexAiSearchTool
from google.genai import types

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
search_tool = VertexAiSearchTool(data_store_id=DATASTORE_PATH)

# Primary RAG FAQ Agent with multi-turn memory & general knowledge fallback
faq_assistant_agent = Agent(
    name="faq_assistant_agent",
    model="gemini-2.5-pro",
    instruction="""
You are an official customer support FAQ assistant.

INSTRUCTIONS:
1. You MUST ALWAYS search the Vertex AI Agent Search datastore first using your search tool for specific company policies and support tickets.
2. CONTEXTUAL FOLLOW-UP SEARCH RULE: When the user's message is a short follow-up or answer to your previous clarifying question (e.g. specifying a brand name like 'Newegg' or 'Apple' or product category), look at the CONVERSATION HISTORY and combine the customer's original question with their follow-up answer to execute a full, targeted search query (e.g. search for 'Newegg return policy') using your search tool.
3. You MUST summarize and synthesize ALL relevant support records, brand policies, and resolution guidelines retrieved from the datastore to answer the customer's query directly and thoroughly.
4. DO NOT refuse to answer or decline to provide details simply because the retrieved records originate from customer service logs or historical support tickets. Always extract and present all actionable policy details (e.g. RMA requirements, return windows, restocking fees, return shipping terms, seller vs manufacturer policies) present in the datastore results.
5. CLARIFICATION & LOW CONFIDENCE RULE:
   - If you have low confidence in the retrieved answer,
   - OR if the customer's question is underspecified, ambiguous, or missing key context (such as specific item type, brand, or order state),
   - OR if the retrieved datastore documents diverge or contradict each other across different policies or categories,
   you MUST explicitly ask polite, targeted clarifying follow-up questions in your response to help narrow down the exact policy, product, or order context.
6. If NO matching support records are found in the datastore for the customer's query, use your general knowledge to provide a helpful support response while offering to clarify further if needed.
7. Maintain continuity across previous conversation turns.

STRICT JSON OUTPUT FORMAT:
Output must strictly adhere to valid JSON format with keys:
- answer: string (Your detailed support answer synthesized from datastore records, including any clarifying follow-up questions)
- citations: array of strings (List of document names, entry IDs, or search queries used to derive the answer)
- grounded: boolean (true if derived from datastore results, false otherwise)
""",
    tools=[search_tool]
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
        if answer_text:
            try:
                data = json.loads(answer_text, strict=False)
                if isinstance(data, dict):
                    final_answer = data.get("answer", answer_text)
                    is_grounded = data.get("grounded", True)
                    agent_cits = data.get("citations", [])
                    for c in agent_cits:
                        if str(c) not in citations:
                            citations.append(str(c))
            except Exception:
                import re
                match = re.search(r'"answer":\s*"(.*?)"\s*,\s*"(?:citations|grounded)"', answer_text, re.DOTALL)
                if match:
                    final_answer = match.group(1).replace("\\n", "\n").replace('\\"', '"')

        if not citations:
            citations.append(f"Vertex AI Agent Search Datastore ({DATASTORE_PATH})")

        return {
            "answer": final_answer if final_answer else "No matching support records found in the datastore for your query.",
            "citations": citations,
            "grounded": is_grounded,
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
