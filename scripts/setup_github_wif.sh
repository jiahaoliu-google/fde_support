#!/usr/bin/env bash
# One-time setup: let GitHub Actions in this repo authenticate to GCP with Workload
# Identity Federation (short-lived OIDC tokens, no service-account JSON keys).
#
# Creates (idempotent, safe to re-run):
#   * a CI-only service account with the minimum roles the PR eval needs:
#       roles/aiplatform.user                   Gemini calls (agents + LLM judges, eval service)
#       roles/discoveryengine.viewer            search_support_records (servingConfigs.search)
#       roles/serviceusage.serviceUsageConsumer bill API quota to the project
#   * a workload identity pool + GitHub OIDC provider that ONLY accepts tokens from
#     ${GITHUB_REPO} (attribute condition), and lets that repo impersonate the SA.
#
# Usage:  scripts/setup_github_wif.sh            (defaults below)
#         PROJECT_ID=... GITHUB_REPO=owner/name scripts/setup_github_wif.sh
# Then add the two printed values as GitHub repository secrets.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
GITHUB_REPO="${GITHUB_REPO:-$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')}"
POOL_ID="${POOL_ID:-github-actions}"
PROVIDER_ID="${PROVIDER_ID:-github-oidc}"
SA_NAME="${SA_NAME:-fde-support-ci-eval-sa}"

[[ -n "$PROJECT_ID" && "$GITHUB_REPO" == */* ]] || { echo "Set PROJECT_ID and GITHUB_REPO=owner/name" >&2; exit 1; }
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
POOL_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}"

echo "Project: $PROJECT_ID ($PROJECT_NUMBER)"
echo "Repo:    $GITHUB_REPO"
echo

echo "== APIs"
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  --project "$PROJECT_ID"

echo "== Service account $SA_EMAIL"
if ! gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \
    --display-name "FDE Support CI eval (GitHub Actions)" \
    --description "Used only by .github/workflows/eval-pr.yml via Workload Identity Federation"
fi
for role in roles/aiplatform.user roles/discoveryengine.viewer roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${SA_EMAIL}" --role "$role" --condition None --quiet >/dev/null
  echo "  granted $role"
done

echo "== Workload identity pool $POOL_ID"
if ! gcloud iam workload-identity-pools describe "$POOL_ID" --location global --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL_ID" --location global --project "$PROJECT_ID" \
    --display-name "GitHub Actions"
fi

echo "== OIDC provider $PROVIDER_ID (only $GITHUB_REPO)"
if ! gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" --workload-identity-pool "$POOL_ID" \
     --location global --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
    --workload-identity-pool "$POOL_ID" --location global --project "$PROJECT_ID" \
    --display-name "GitHub OIDC" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref,attribute.event_name=assertion.event_name" \
    --attribute-condition "assertion.repository == '${GITHUB_REPO}'"
fi

echo "== Allow $GITHUB_REPO to impersonate $SA_EMAIL"
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project "$PROJECT_ID" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/${POOL_NAME}/attribute.repository/${GITHUB_REPO}" --quiet >/dev/null

PROVIDER_NAME="$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --workload-identity-pool "$POOL_ID" --location global --project "$PROJECT_ID" --format 'value(name)')"

cat <<EOF

Done. Add these GitHub repository secrets
(https://github.com/${GITHUB_REPO}/settings/secrets/actions -> New repository secret):

  GCP_WORKLOAD_IDENTITY_PROVIDER = ${PROVIDER_NAME}
  GCP_SERVICE_ACCOUNT            = ${SA_EMAIL}

IAM changes can take a few minutes to propagate before the first workflow run succeeds.
EOF
