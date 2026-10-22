#!/usr/bin/env bash
# Gate 3: Post-build local container boot and endpoint verification.
#
# Starts each of the 4 built Docker images on loopback (127.0.0.1) in offline mode
# and verifies that:
#   1. All Python/Node dependencies and entrypoints start cleanly.
#   2. Both FastAPI web services (backend, frontend) respond 200 OK on /health.
#   3. Both ADK Agent Runtime containers (faq-assistant, sentiment-router) respond
#      200 OK on /health, /list-apps?detailed=true (forcing lazy import of agent.py
#      and tools.py), and POST /api/reasoning_engine (list_sessions).
set -euo pipefail

BACKEND_IMAGE="${BACKEND_IMAGE:?BACKEND_IMAGE must be set}"
FRONTEND_IMAGE="${FRONTEND_IMAGE:?FRONTEND_IMAGE must be set}"
FAQ_IMAGE="${FAQ_IMAGE:?FAQ_IMAGE must be set}"
SENTIMENT_IMAGE="${SENTIMENT_IMAGE:?SENTIMENT_IMAGE must be set}"

CONTAINERS=(
  "ci-boot-backend"
  "ci-boot-frontend"
  "ci-boot-faq"
  "ci-boot-sentiment"
)

cleanup() {
  for c in "${CONTAINERS[@]}"; do
    docker rm -f "$c" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

wait_for_http_200() {
  local name="$1"
  local url="$2"
  local max_wait="${3:-25}"
  local elapsed=0

  echo "⏳ Waiting for ${name} at ${url} (timeout=${max_wait}s)..."
  while [ "$elapsed" -lt "$max_wait" ]; do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      echo "✅ ${name} responded 200 OK at ${url}"
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  echo "❌ ${name} failed to respond at ${url} within ${max_wait}s. Container logs:" >&2
  docker logs "$name" >&2 || true
  return 1
}

cleanup

echo "============================================================"
echo " 1/4 Boot-testing Backend container (${BACKEND_IMAGE})"
echo "============================================================"
docker run -d --name ci-boot-backend \
  -p 127.0.0.1:18000:8000 \
  -e PORT=8000 \
  -e ENV=development \
  -e DATABASE_URL="sqlite:///./ci_smoke_boot.db" \
  -e OTEL_TRACES_EXPORTER="none" \
  -e MODEL_ARMOR_ENABLED="false" \
  -e MODEL_ARMOR_DLP_ENABLED="false" \
  "$BACKEND_IMAGE"

wait_for_http_200 "ci-boot-backend" "http://127.0.0.1:18000/health" 25
curl -fsS "http://127.0.0.1:18000/health" | grep -q '"healthy"'

echo "============================================================"
echo " 2/4 Boot-testing Frontend container (${FRONTEND_IMAGE})"
echo "============================================================"
docker run -d --name ci-boot-frontend \
  -p 127.0.0.1:13000:3000 \
  -e PORT=3000 \
  -e CLOUD_RUN_BACKEND_URL="http://127.0.0.1:18000" \
  "$FRONTEND_IMAGE"

wait_for_http_200 "ci-boot-frontend" "http://127.0.0.1:13000/health" 25
curl -fsS "http://127.0.0.1:13000/health" | grep -q '"healthy"'
curl -fsS "http://127.0.0.1:13000/" | grep -qi "<!doctype html"

echo "============================================================"
echo " 3/4 Boot-testing FAQ Assistant container (${FAQ_IMAGE})"
echo "============================================================"
docker run -d --name ci-boot-faq \
  -p 127.0.0.1:18081:8080 \
  -e PORT=8080 \
  -e GOOGLE_CLOUD_PROJECT="fde-support-sandbox-dev-jliu" \
  -e OTEL_TRACES_EXPORTER="none" \
  -e GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY="false" \
  -e REQUIRE_DURABLE_SESSIONS="false" \
  "$FAQ_IMAGE"

wait_for_http_200 "ci-boot-faq" "http://127.0.0.1:18081/health" 25
FAQ_APPS="$(curl -fsS "http://127.0.0.1:18081/list-apps?detailed=true")"
echo "FAQ /list-apps?detailed=true -> ${FAQ_APPS}"
echo "$FAQ_APPS" | grep -Eq '"faq_assistant(_agent)?"'
curl -fsS -X POST "http://127.0.0.1:18081/api/reasoning_engine" \
  -H "Content-Type: application/json" \
  -d '{"class_method": "list_sessions", "input": {"user_id": "ci_boot_probe"}}' | grep -q '"sessions"'

echo "============================================================"
echo " 4/4 Boot-testing Sentiment Router container (${SENTIMENT_IMAGE})"
echo "============================================================"
docker run -d --name ci-boot-sentiment \
  -p 127.0.0.1:18082:8080 \
  -e PORT=8080 \
  -e GOOGLE_CLOUD_PROJECT="fde-support-sandbox-dev-jliu" \
  -e OTEL_TRACES_EXPORTER="none" \
  -e GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY="false" \
  "$SENTIMENT_IMAGE"

wait_for_http_200 "ci-boot-sentiment" "http://127.0.0.1:18082/health" 25
SENTIMENT_APPS="$(curl -fsS "http://127.0.0.1:18082/list-apps?detailed=true")"
echo "Sentiment /list-apps?detailed=true -> ${SENTIMENT_APPS}"
echo "$SENTIMENT_APPS" | grep -Eq '"sentiment_router(_agent)?"'
curl -fsS -X POST "http://127.0.0.1:18082/api/reasoning_engine" \
  -H "Content-Type: application/json" \
  -d '{"class_method": "list_sessions", "input": {"user_id": "ci_boot_probe"}}' | grep -q '"sessions"'

echo "✅ All 4 built containers passed local boot and endpoint verification!"
