#!/usr/bin/env python3
"""
Method 2 CLI Client:
Submits a support query to fde-support-frontend on Cloud Run using gcloud OIDC tokens.
"""

import sys
import subprocess
import requests

FRONTEND_URL = "https://fde-support-frontend-883790454740.us-central1.run.app"

def get_id_token() -> str:
    try:
        return subprocess.check_output(
            ["gcloud", "auth", "print-identity-token"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
    except Exception as e:
        print(f"Error fetching gcloud identity token: {e}")
        sys.exit(1)

def query_chat(message: str):
    token = get_id_token()
    url = f"{FRONTEND_URL}/api/chat"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "message": message,
        "customer_id": "cli_user_001"
    }
    
    print(f"Sending query to {url}...")
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    print(f"Status Code: {resp.status_code}")
    
    if resp.status_code == 200:
        data = resp.json()
        print("\n--- Support Response ---")
        print(f"Ticket ID: {data.get('ticket_id')}")
        print(f"Status:    {data.get('ticket_status')}")
        print(f"Sentiment: {data.get('sentiment')}")
        print(f"Routed To: {data.get('routed_to')}")
        print(f"Answer:\n{data.get('answer')}")
        print(f"\nCitations: {data.get('citations')}")
    else:
        print(f"Error: {resp.text}")

if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "What is the Best Buy return policy?"
    query_chat(prompt)
