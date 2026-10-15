#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="fde-support-sandbox-dev-jliu"
REGION="us-central1"
PATH="$PATH:/Users/jiahaoliu/.local/bin"

echo "============================================================"
echo " Deploying FAQ Assistant Agent (gemini-2.5-pro) "
echo "============================================================"

cd "$(dirname "$0")/../agents/faq_assistant"

agents-cli deploy \
    -d agent_runtime \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --no-confirm-project

echo "=== FAQ Assistant Agent Deployment Command Triggered Successfully! ==="
