import os
import json
import logging
from typing import Dict, Any

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "fde-support-sandbox-dev-jliu")

# Ensure Vertex AI and ADK Telemetry environment variables for Agent Runtime
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] = "true"
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "SPAN_AND_EVENT"
os.environ.setdefault("OTEL_SERVICE_NAME", "fde-support-sentiment-router")
os.environ.setdefault("OTEL_RESOURCE_ATTRIBUTES", "service.name=fde-support-sentiment-router")

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

session_service = InMemorySessionService()

# Explicit ADK Agent Object Instance using Vertex AI
sentiment_router_agent = Agent(
    name="sentiment_router_agent",
    model="gemini-2.5-flash",
    instruction="""
You are an AI support ticket routing assistant.
Classify incoming customer support tickets into sentiment categories ('URGENT_FRUSTRATED' or 'NEUTRAL_FACTUAL')
and extract key entities (order_id, product_name, customer email).

STRICT CLASSIFICATION CRITERIA:
1. Default to 'NEUTRAL_FACTUAL' for standard questions, status checks, return inquiries, feature requests, or mild concerns (e.g. "Where is my order?", "Can I return this?", "My package is 1 day late").
2. Reserve 'URGENT_FRUSTRATED' ONLY for severe, high-escalation situations. A query MUST satisfy at least one of the following:
   - Explicit emotional rage or severe anger (e.g. ALL CAPS shouting, swearing, explicit statements like "I am furious!", "unacceptable service!").
   - Critical financial/billing emergencies (e.g. "I was charged twice", "unauthorized billing", "money stolen").
   - Severe service outages or safety hazards preventing core functionality.

Output must strictly adhere to valid JSON format with keys:
- sentiment: string ("URGENT_FRUSTRATED" or "NEUTRAL_FACTUAL")
- order_id: string or null
- product_name: string or null
- email: string or null
""",
    generate_content_config=types.GenerateContentConfig(
        response_mime_type="application/json"
    )
)

app = App(
    name="sentiment_router_app",
    root_agent=sentiment_router_agent
)

def analyze_ticket_sentiment(ticket_text: str) -> Dict[str, Any]:
    """
    Executes the Sentiment Router Agent using Google ADK Runner with auto_create_session=True.
    """
    prompt = f"Analyze the following customer support ticket and return sentiment analysis JSON:\n\n{ticket_text}"
    
    try:
        runner = Runner(
            app_name="fde_support_app",
            agent=sentiment_router_agent,
            session_service=session_service,
            auto_create_session=True
        )
        
        msg = types.Content(parts=[types.Part.from_text(text=prompt)])
        
        response_text = ""
        for event in runner.run(
            user_id="user_default",
            session_id=f"session_sentiment_{abs(hash(ticket_text))}",
            new_message=msg
        ):
            if hasattr(event, "content") and event.content:
                if hasattr(event.content, "parts"):
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            response_text += part.text
            elif hasattr(event, "text"):
                response_text += event.text

        data = json.loads(response_text) if response_text else {}
        return {
            "sentiment": data.get("sentiment", "NEUTRAL_FACTUAL"),
            "order_id": data.get("order_id"),
            "product_name": data.get("product_name"),
            "email": data.get("email")
        }
    except Exception as e:
        logger.error(f"Error executing ADK Sentiment Router agent: {e}")
        return {
            "sentiment": "NEUTRAL_FACTUAL",
            "order_id": None,
            "product_name": None,
            "email": None
        }
