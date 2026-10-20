#!/usr/bin/env bash
# One-time bootstrap of the Terraform remote-state bucket, then migrate local state into it.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-fde-support-sandbox-dev-jliu}"
REGION="${REGION:-us-central1}"
BUCKET="fde-support-tfstate-${PROJECT_ID}"

if ! gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi
gcloud storage buckets update "gs://${BUCKET}" --versioning

cd "$(dirname "$0")/../../deployment/terraform"
terraform init -migrate-state -force-copy
echo "State now in gs://${BUCKET}/fde-support/sandbox"
