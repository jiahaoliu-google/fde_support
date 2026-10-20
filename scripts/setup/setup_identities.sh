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

# 3. Grant Backend Service Account Cloud SQL Client, Vertex AI User, and Secret Manager Accessor roles
echo "Step 3: Assigning IAM roles to Backend Service Account..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${BACKEND_SA}" \
    --role="roles/cloudsql.client" \
    --quiet

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${BACKEND_SA}" \
    --role="roles/aiplatform.user" \
    --quiet

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${BACKEND_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet

# PII redaction (backend/security/model_armor.py) calls Cloud DLP deidentify_content.
gcloud services enable dlp.googleapis.com --project="${PROJECT_ID}"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${BACKEND_SA}" \
    --role="roles/dlp.user" \
    --quiet

# Model Armor sanitizeUserPrompt (PII de-identification + prompt-injection screening) runs
# first; the template itself is managed by Terraform (deployment/terraform/agent_gateway.tf).
gcloud services enable modelarmor.googleapis.com --project="${PROJECT_ID}"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${BACKEND_SA}" \
    --role="roles/modelarmor.user" \
    --quiet

# 4. Grant Frontend Service Account Cloud Run Invoker on the BACKEND SERVICE ONLY.
# SECURITY: the backend takes the user's identity from the IAP-signed JWT the frontend forwards
# (accepted only from the frontend SA), or from a direct invoker's own ID token (acts as itself).
# It never trusts X-User-Email. See backend/security/caller_identity.py.
echo "Step 4: Scoping Cloud Run Invoker on backend service to Frontend Service Account..."
REGION="us-central1"
BACKEND_SERVICE="fde-support-backend"

gcloud run services add-iam-policy-binding "${BACKEND_SERVICE}" \
    --region="${REGION}" \
    --member="serviceAccount:${FRONTEND_SA}" \
    --role="roles/run.invoker" \
    --project="${PROJECT_ID}" \
    --quiet

# Remove legacy broad grants (ignore errors if they are already absent).
gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${FRONTEND_SA}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null 2>&1 || true

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
gcloud run services remove-iam-policy-binding "${BACKEND_SERVICE}" \
    --region="${REGION}" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/run.invoker" \
    --project="${PROJECT_ID}" \
    --quiet >/dev/null 2>&1 || true

echo "============================================================"
echo " SUCCESS: Service Accounts & IAM Roles Configured! "
echo " Frontend SA : ${FRONTEND_SA}"
echo " Backend SA  : ${BACKEND_SA}"
echo "============================================================"
