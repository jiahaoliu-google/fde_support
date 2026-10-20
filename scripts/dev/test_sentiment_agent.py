import os
import sys
import certifi
import json

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Ensure SSL certificate environment variables
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

from agents.sentiment_router.agent import analyze_ticket_sentiment

def run_sentiment_tests():
    test_tickets = [
        {
            "id": 1,
            "description": "Urgent & Frustrated Ticket",
            "text": "THIS IS UNACCEPTABLE! My order #ORD-98213 for the iPhone 15 Pro was charged twice and I haven't received confirmation! Fix this NOW or I am calling my credit card company! Contact me at alex.smith@example.com immediately!"
        },
        {
            "id": 2,
            "description": "Neutral & Factual Inquiry",
            "text": "Hello, I am checking the warranty details for my Sony WH-1000XM5 headphones purchased under order #99201. Could you send the policy to user@domain.com?"
        },
        {
            "id": 3,
            "description": "General Support Inquiry",
            "text": "I am having trouble connecting my Spotify Premium account to my bluetooth speaker. Can someone help me troubleshoot this?"
        }
    ]

    print("=" * 60)
    print(" Testing ADK Sentiment Router Agent")
    print("=" * 60)

    for ticket in test_tickets:
        print(f"\n[{ticket['id']}] Test Case: {ticket['description']}")
        print(f"Ticket Input: '{ticket['text']}'")
        
        result = analyze_ticket_sentiment(ticket["text"])
        
        print("\n--- Extracted Analysis ---")
        print(f"Sentiment Classification : {result.get('sentiment')}")
        print("Extracted Entities       :")
        print(json.dumps(result.get("extracted_entities", {}), indent=4))
        print("Raw Agent Output         :")
        print(json.dumps(result.get("raw_response", {}), indent=4))
        print("=" * 60)

if __name__ == "__main__":
    run_sentiment_tests()
