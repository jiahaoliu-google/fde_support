#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="fde-support-sandbox-dev-jliu"
REGION="us-central1"
SERVICE_NAME="fde-support-backend"
REPO_NAME="fde-support-repo"
IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/backend:latest"
SERVICE_ACCOUNT="fde-support-backend-sa@${PROJECT_ID}.iam.gserviceaccount.com"
# Caller identity (backend/caller_identity.py). Same IAP audience as frontend/deploy.sh.
IAP_AUDIENCE="${IAP_AUDIENCE:-/projects/883790454740/global/backendServices/6965840051173915797}"
FRONTEND_SERVICE_ACCOUNT="fde-support-frontend-sa@${PROJECT_ID}.iam.gserviceaccount.com"
# Testing: principals with run.invoker may call the backend URL directly, acting as themselves.
ALLOW_DIRECT_CALLERS="${ALLOW_DIRECT_CALLERS:-true}"

echo "============================================================"
echo " Building & Deploying Backend Service to Cloud Run "
echo " Project         : ${PROJECT_ID} "
echo " Region          : ${REGION} "
echo " Service Account : ${SERVICE_ACCOUNT} "
echo "============================================================"

# Navigate to repo root to access full build context
cd "$(dirname "$0")/.."

# Step 1: Submit Cloud Build job to build and push container image
echo "Step 1: Building container image via Cloud Build..."
gcloud builds submit \
    --region="${REGION}" \
    --gcs-source-staging-dir="gs://run-sources-${PROJECT_ID}-${REGION}/source" \
    --gcs-log-dir="gs://run-sources-${PROJECT_ID}-${REGION}/logs" \
    --config=backend/cloudbuild.yaml . \
    --project="${PROJECT_ID}"

# Step 2: Deploy to Cloud Run with dedicated Service Account
echo "Step 2: Deploying to Cloud Run with dedicated identity..."
gcloud run deploy "${SERVICE_NAME}" \
    --image="${IMAGE_TAG}" \
    --platform=managed \
    --region="${REGION}" \
    --service-account="${SERVICE_ACCOUNT}" \
    --no-allow-unauthenticated \
    --update-env-vars="IAP_AUDIENCE=${IAP_AUDIENCE},FRONTEND_SERVICE_ACCOUNT=${FRONTEND_SERVICE_ACCOUNT},ALLOW_DIRECT_CALLERS=${ALLOW_DIRECT_CALLERS}" \
    --project="${PROJECT_ID}"

echo "=== Backend Deployment Completed Successfully! ==="
