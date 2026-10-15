#!/bin/bash
set -e

# Configuration
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-fde-support-sandbox-dev-jliu}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
REPO_NAME="fde-support-repo"
REGISTRY_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}"

echo "================================================================="
echo "  Google Artifact Registry Build & Push Script"
echo "  GCP Project : ${PROJECT_ID}"
echo "  Region      : ${REGION}"
echo "  Repository  : ${REGISTRY_URL}"
echo "================================================================="

# Step 1: Enable Artifact Registry API
echo "Step 1: Enabling artifactregistry.googleapis.com..."
gcloud services enable artifactregistry.googleapis.com --project="${PROJECT_ID}"

# Step 2: Create Artifact Registry Repository if it doesn't exist
echo "Step 2: Checking Artifact Registry repository '${REPO_NAME}'..."
if ! gcloud artifacts repositories describe "${REPO_NAME}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "Creating repository '${REPO_NAME}' in ${REGION}..."
    gcloud artifacts repositories create "${REPO_NAME}" \
        --repository-format=docker \
        --location="${REGION}" \
        --description="Docker container repository for FDE Support microservices" \
        --project="${PROJECT_ID}"
else
    echo "Repository '${REPO_NAME}' already exists."
fi

# Step 3: Configure Docker Authentication for GCP Artifact Registry
echo "Step 3: Configuring Docker authentication for ${REGION}-docker.pkg.dev..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# Step 4: Build, Tag, and Push Container Images
echo "-----------------------------------------------------------------"
echo "Building, Tagging, and Pushing Frontend Image..."
echo "-----------------------------------------------------------------"
docker build -t "${REGISTRY_URL}/frontend:latest" -f frontend/Dockerfile .
docker push "${REGISTRY_URL}/frontend:latest"

echo "-----------------------------------------------------------------"
echo "Building, Tagging, and Pushing Backend Image..."
echo "-----------------------------------------------------------------"
docker build -t "${REGISTRY_URL}/backend:latest" -f backend/Dockerfile .
docker push "${REGISTRY_URL}/backend:latest"

echo "-----------------------------------------------------------------"
echo "Building, Tagging, and Pushing Agents (FAQ & Sentiment) Image..."
echo "-----------------------------------------------------------------"
docker build -t "${REGISTRY_URL}/agent-service:latest" -f agents/Dockerfile .
docker push "${REGISTRY_URL}/agent-service:latest"

echo "================================================================="
echo "  SUCCESS: All container images pushed to Artifact Registry!"
echo "  Frontend Image      : ${REGISTRY_URL}/frontend:latest"
echo "  Backend Image       : ${REGISTRY_URL}/backend:latest"
echo "  Agent-Service Image : ${REGISTRY_URL}/agent-service:latest"
echo "================================================================="
