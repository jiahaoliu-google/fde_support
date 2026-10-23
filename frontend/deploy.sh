#!/usr/bin/env bash
# Build the frontend image and roll it out. IMAGE ONLY: env vars (backend URL, IAP audience),
# IAM and ingress are owned by Terraform (deployment/terraform/cloud_run.tf), which keeps
# whatever image is live (hybrid mode), so this script and `terraform apply` never fight.
#
#   ./frontend/deploy.sh               build + roll the new image (code changes)
#   ./frontend/deploy.sh --build-only  build only; prints the digest for
#                                      `terraform apply -var frontend_image=<digest>`
set -euo pipefail

PROJECT_ID="fde-support-sandbox-dev-jliu"
REGION="us-central1"
SERVICE_NAME="fde-support-frontend"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/fde-support-repo/frontend"

BUILD_ONLY=false
[[ "${1:-}" == "--build-only" ]] && BUILD_ONLY=true

cd "$(dirname "$0")/.."

# Immutable tag: git SHA (+ "-dirty-<time>" for uncommitted builds, so it never collides).
TAG="$(git rev-parse --short HEAD)"
if [[ -n "$(git -c core.fsmonitor=false status --porcelain -- frontend)" ]]; then
  TAG="${TAG}-dirty-$(date +%Y%m%d%H%M%S)"
fi

echo "== Building ${IMAGE}:${TAG} via Cloud Build"
gcloud builds submit . \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --config=frontend/cloudbuild.yaml \
    --substitutions="_TAG=${TAG}" \
    --gcs-source-staging-dir="gs://run-sources-${PROJECT_ID}-${REGION}/source" \
    --gcs-log-dir="gs://run-sources-${PROJECT_ID}-${REGION}/logs"

DIGEST="$(gcloud artifacts docker images describe "${IMAGE}:${TAG}" \
    --project="${PROJECT_ID}" --format='value(image_summary.digest)')"
IMAGE_REF="${IMAGE}@${DIGEST}"
echo "== Built ${IMAGE_REF}"

if [[ "${BUILD_ONLY}" == true ]]; then
  echo "${IMAGE_REF}" > "$(dirname "$0")/.last_image"
  echo "Pin it with: terraform apply -var frontend_image=${IMAGE_REF}"
  exit 0
fi

echo "== Rolling ${SERVICE_NAME} to ${IMAGE_REF}"
gcloud run services update "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${IMAGE_REF}"

echo "=== Frontend deployed: ${IMAGE_REF}"
