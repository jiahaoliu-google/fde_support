#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="fde-support-sandbox-dev-jliu"
REGION="us-central1"
PATH="$PATH:/Users/jiahaoliu/.local/bin"

echo "============================================================"
echo " Deploying FAQ Assistant Agent with Agent Identity "
echo "============================================================"

cd "$(dirname "$0")"

agents-cli deploy \
    -d agent_runtime \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --agent-identity \
    --no-confirm-project

echo "=== FAQ Assistant Agent Deployment Command Triggered Successfully! ==="
