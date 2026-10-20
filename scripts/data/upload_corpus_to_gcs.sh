#!/usr/bin/env bash
# Script to upload archive/sample.jsonl to GCS and trigger Vertex AI Agent Search ingestion

set -euo pipefail

PROJECT_ID="${1:-fde-support-sandbox-dev-jliu}"
BUCKET_NAME="fde-support-faq-docs-${PROJECT_ID}"
DATASTORE_ID="fde-support-faq-datastore"

echo "=== 1. Converting CSV to JSONL ==="
python3 scripts/convert_csv_to_jsonl.py

echo "=== 2. Uploading archive/sample.jsonl to gs://${BUCKET_NAME}/ ==="
gcloud storage cp "archive/sample.jsonl" "gs://${BUCKET_NAME}/sample.jsonl" --project="${PROJECT_ID}"

echo "=== 3. Importing JSONL documents into Vertex AI Agent Search Data Store '${DATASTORE_ID}' ==="
gcloud discovery-engine datastores import-documents "${DATASTORE_ID}" \
  --location="global" \
  --gcs-uri="gs://${BUCKET_NAME}/sample.jsonl" \
  --reconciliation-mode="INCREMENTAL" \
  --project="${PROJECT_ID}"

echo "=== Complete! Synced to GCS and Vertex AI Agent Search RAG ==="
