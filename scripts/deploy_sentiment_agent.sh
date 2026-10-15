#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="fde-support-sandbox-dev-jliu"
REGION="us-central1"
PATH="$PATH:/Users/jiahaoliu/.local/bin"

echo "============================================================"
echo " Deploying Sentiment Router Agent (gemini-2.5-flash) "
echo "============================================================"

cd "$(dirname "$0")/../agents/sentiment_router"

agents-cli deploy \
    -d agent_runtime \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --no-confirm-project

echo "=== Sentiment Router Agent Deployment Command Triggered Successfully! ==="
