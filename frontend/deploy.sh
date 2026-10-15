#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="fde-support-sandbox-dev-jliu"
REGION="us-central1"
SERVICE_NAME="fde-support-frontend"
REPO_NAME="fde-support-repo"
IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/frontend:latest"
SERVICE_ACCOUNT="fde-support-frontend-sa@${PROJECT_ID}.iam.gserviceaccount.com"

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
gcloud builds submit --tag "${IMAGE_TAG}" -f frontend/Dockerfile . --project="${PROJECT_ID}"

# Step 2: Deploy to Cloud Run with dedicated Service Account
echo "Step 2: Deploying to Cloud Run with dedicated identity..."
gcloud run deploy "${SERVICE_NAME}" \
    --image="${IMAGE_TAG}" \
    --platform=managed \
    --region="${REGION}" \
    --service-account="${SERVICE_ACCOUNT}" \
    --allow-unauthenticated \
    --project="${PROJECT_ID}"

echo "=== Frontend Deployment Completed Successfully! ==="
