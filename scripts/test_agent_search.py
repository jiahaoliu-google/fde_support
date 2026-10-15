#!/usr/bin/env python3
"""
Test script to verify Vertex AI Agent Search datastore retrieval and grounded FAQ response generation.
"""

import os
import sys
import json

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.faq_assistant.agent import answer_faq

def test_faq_agent_search():
    sample_queries = [
        "My phone is running very slow after updating to the latest iOS",
        "Spotify Premium is skipping songs constantly when connected to my bluetooth speaker",
        "I need help with my flight delay status"
    ]

    print("============================================================")
    print(" Testing Vertex AI Agent Search RAG Integration")
    print("============================================================\n")

    for idx, query in enumerate(sample_queries, 1):
        print(f"[{idx}] Customer Query: '{query}'")
        try:

            result = answer_faq(query)
            print("--- Generated Response ---")
            print(result.get("answer"))
            print("--- Grounding Citations ---")
            for citation in result.get("citations", []):
                print(f"  - {citation}")
            print(f"Grounded Status: {result.get('grounded')}")
            print("============================================================\n")
        except Exception as e:
            print(f"Error invoking FAQ agent: {e}")
            print("============================================================\n")

if __name__ == "__main__":
    test_faq_agent_search()
