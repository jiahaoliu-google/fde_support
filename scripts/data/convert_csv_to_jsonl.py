#!/usr/bin/env python3
"""
Converts archive/twcs/twcs.csv into Vertex AI Agent Search compatible archive/twcs/twcs.jsonl format.
Links inbound customer tweets directly to their brand reply texts via response_tweet_id so that Vertex AI Agent Search RAG
retrieves the authoritative brand response reference for grounded answer generation.
"""

import csv
import json
import os

CSV_PATH = os.path.join(os.path.dirname(__file__), "../archive/twcs/twcs.csv")
JSONL_PATH = os.path.join(os.path.dirname(__file__), "../archive/twcs/twcs.jsonl")

def convert_csv_to_jsonl():
    if not os.path.exists(CSV_PATH):
        print(f"Error: {CSV_PATH} not found.")
        return

    tweets_by_id = {}
    documents = []

    # Read all CSV rows
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        for row in rows:
            tweets_by_id[row["tweet_id"]] = row

    for row in rows:
        # For inbound customer tweets, find their corresponding brand reply via response_tweet_id
        if row.get("inbound", "").lower() == "true":
            response_text = ""
            response_author = ""
            resp_id = ""

            if row.get("response_tweet_id"):
                resp_ids = [r.strip() for r in row["response_tweet_id"].split(",")]
                for r in resp_ids:
                    if r in tweets_by_id:
                        resp_tweet = tweets_by_id[r]
                        response_text = resp_tweet["text"]
                        response_author = resp_tweet["author_id"]
                        resp_id = r
                        break

            doc = {
                "id": f"qa_{row['tweet_id']}",
                "customer_tweet_id": row["tweet_id"],
                "customer_question": row["text"],
                "response_tweet_id": resp_id,
                "brand_author": response_author,
                "reference_brand_response_text": response_text,
                "content": (
                    f"Customer Feed / Question: {row['text']}\n"
                    f"Mapped Response Tweet ({response_author} #{resp_id}): {response_text if response_text else 'Refer to standard customer support guidance.'}"
                )
            }
            documents.append(doc)

    # Write out as JSON Lines (.jsonl)
    with open(JSONL_PATH, "w", encoding="utf-8") as f:
        for doc in documents:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    print(f"Successfully created {len(documents)} Q&A linked documents at {JSONL_PATH}")

if __name__ == "__main__":
    convert_csv_to_jsonl()
