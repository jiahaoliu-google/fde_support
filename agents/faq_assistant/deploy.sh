#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-fde-support-sandbox-dev-jliu}"
REGION="${REGION:-us-central1}"
ENGINE_ID="${FAQ_ENGINE_ID:-projects/883790454740/locations/us-central1/reasoningEngines/6767283237606653952}"
PATH="$PATH:/Users/jiahaoliu/.local/bin"

echo "============================================================"
echo " Deploying FAQ Assistant Agent with Agent Identity "
echo "============================================================"

cd "$(dirname "$0")"

if [ ! -f deployment_metadata.json ]; then
  cat > deployment_metadata.json <<EOF
{
  "remote_agent_runtime_id": "${ENGINE_ID}",
  "deployment_target": "agent_runtime",
  "is_a2a": false,
  "agent_directory": "."
}
EOF
fi

agents-cli deploy \
    -d agent_runtime \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --agent-identity \
    --update-env-vars="FAQ_SESSION_TTL_SECONDS=604800,REQUIRE_DURABLE_SESSIONS=true,AGENT_ENGINE_LOCATION=${REGION}" \
    --no-confirm-project

echo "=== FAQ Assistant Agent Deployment Command Triggered Successfully! ==="
