import os
import json
import logging
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

CSV_PATH = os.path.join("archive", "twcs", "twcs.csv")
OUTPUT_JSONL_PATH = os.path.join("archive", "twcs", "twcs.jsonl")

def convert_twcs_csv_to_jsonl(csv_path: str, jsonl_path: str, max_records: int = 100000) -> int:
    """
    Converts twcs.csv into Discovery Engine NO_CONTENT JSONL format.
    Pairs inbound customer questions with official company responses.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Source CSV file not found at: {csv_path}")

    logger.info(f"Reading CSV dataset from {csv_path}...")
    df = pd.read_csv(csv_path, low_memory=False)
    
    # Filter inbound customer tweets and outbound company responses
    inbound_df = df[df["inbound"] == True].copy()
    outbound_df = df[df["inbound"] == False].copy()

    # Merge customer questions with official brand responses via response_tweet_id
    inbound_df["response_tweet_id_clean"] = inbound_df["response_tweet_id"].fillna("").astype(str).str.split(",").str[0]
    outbound_df["tweet_id_str"] = outbound_df["tweet_id"].astype(str)

    merged = pd.merge(
        inbound_df,
        outbound_df,
        left_on="response_tweet_id_clean",
        right_on="tweet_id_str",
        how="inner",
        suffixes=("_customer", "_response")
    )

    logger.info(f"Successfully matched {len(merged):,} Q&A pairs from raw dataset.")

    if max_records and len(merged) > max_records:
        merged = merged.iloc[:max_records]
        logger.info(f"Subsetting to top {max_records:,} records for datastore optimization.")

    os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)

    count = 0
    with open(jsonl_path, "w", encoding="utf-8") as out_f:
        for _, row in merged.iterrows():
            cust_tweet_id = str(row["tweet_id_customer"])
            resp_tweet_id = str(row["tweet_id_response"])
            cust_text = str(row["text_customer"]).strip()
            resp_text = str(row["text_response"]).strip()
            brand_author = str(row["author_id_response"]).strip()
            created_at = str(row["created_at_customer"])

            doc = {
                "id": f"twcs_{cust_tweet_id}",
                "structData": {
                    "customer_tweet_id": cust_tweet_id,
                    "customer_question": cust_text,
                    "response_tweet_id": resp_tweet_id,
                    "brand_author": brand_author,
                    "reference_brand_response_text": resp_text,
                    "created_at": created_at
                }
            }

            out_f.write(json.dumps(doc, ensure_ascii=False) + "\n")
            count += 1

    logger.info(f"Successfully written {count:,} JSONL documents to {jsonl_path}")
    return count

if __name__ == "__main__":
    convert_twcs_csv_to_jsonl(CSV_PATH, OUTPUT_JSONL_PATH)
