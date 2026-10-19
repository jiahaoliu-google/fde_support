#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="fde-support-sandbox-dev-jliu"
REGION="us-central1"
SERVICE_NAME="fde-support-frontend"
REPO_NAME="fde-support-repo"
IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/frontend:latest"
SERVICE_ACCOUNT="fde-support-frontend-sa@${PROJECT_ID}.iam.gserviceaccount.com"
# Audience of the signed IAP header (x-goog-iap-jwt-assertion) for the IAP-enabled
# load balancer backend service 'fde-frontend-backend-service'.
PROJECT_NUMBER="883790454740"
IAP_BACKEND_SERVICE_ID="6965840051173915797"
IAP_AUDIENCE="${IAP_AUDIENCE:-/projects/${PROJECT_NUMBER}/global/backendServices/${IAP_BACKEND_SERVICE_ID}}"

echo "============================================================"
echo " Building & Deploying Frontend Service to Cloud Run "
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
    --config=cloudbuild-frontend.yaml . \
    --project="${PROJECT_ID}"

# Step 2: Deploy to Cloud Run with dedicated Service Account
echo "Step 2: Deploying to Cloud Run with dedicated identity..."
gcloud run deploy "${SERVICE_NAME}" \
    --image="${IMAGE_TAG}" \
    --platform=managed \
    --region="${REGION}" \
    --service-account="${SERVICE_ACCOUNT}" \
    --no-allow-unauthenticated \
    --update-env-vars="IAP_AUDIENCE=${IAP_AUDIENCE}" \
    --project="${PROJECT_ID}"

echo "=== Frontend Deployment Completed Successfully! ==="
