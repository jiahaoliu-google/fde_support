#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="fde-support-sandbox-dev-jliu"
FRONTEND_SA_NAME="fde-support-frontend-sa"
BACKEND_SA_NAME="fde-support-backend-sa"

FRONTEND_SA="${FRONTEND_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
BACKEND_SA="${BACKEND_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "============================================================"
echo " Provisioning Dedicated Service Accounts for GCP Project "
echo " Project: ${PROJECT_ID}"
echo "============================================================"

# 1. Create Frontend Service Account if it doesn't exist
echo "Step 1: Creating Frontend Service Account '${FRONTEND_SA_NAME}'..."
if ! gcloud iam service-accounts describe "${FRONTEND_SA}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${FRONTEND_SA_NAME}" \
        --display-name="FDE Support Frontend Service Account" \
        --project="${PROJECT_ID}"
    echo "Created ${FRONTEND_SA}"
else
    echo "Service account ${FRONTEND_SA} already exists."
fi

# 2. Create Backend Service Account if it doesn't exist
echo "Step 2: Creating Backend Service Account '${BACKEND_SA_NAME}'..."
if ! gcloud iam service-accounts describe "${BACKEND_SA}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${BACKEND_SA_NAME}" \
        --display-name="FDE Support Backend Service Account" \
        --project="${PROJECT_ID}"
    echo "Created ${BACKEND_SA}"
else
    echo "Service account ${BACKEND_SA} already exists."
fi

# 3. Grant Backend Service Account Cloud SQL Client and Vertex AI User roles
echo "Step 3: Assigning IAM roles to Backend Service Account..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${BACKEND_SA}" \
    --role="roles/cloudsql.client" \
    --quiet

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${BACKEND_SA}" \
    --role="roles/aiplatform.user" \
    --quiet

# 4. Grant Frontend Service Account Cloud Run Invoker on Backend
echo "Step 4: Assigning Cloud Run Invoker role to Frontend Service Account..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${FRONTEND_SA}" \
    --role="roles/run.invoker" \
    --quiet

echo "============================================================"
echo " SUCCESS: Service Accounts & IAM Roles Configured! "
echo " Frontend SA : ${FRONTEND_SA}"
echo " Backend SA  : ${BACKEND_SA}"
echo "============================================================"
