# Service accounts and least-privilege IAM (TDD "Authorization").
#
# The TDD names the runtime identity `support-router-sa`; in this deployment that role is
# played by `fde-support-backend-sa` (same permissions: Cloud SQL Client, Discovery Engine
# Viewer, Secret Manager Secret Accessor - plus what Model Armor / OTel need).
#
# All bindings are additive (google_*_iam_member): Terraform never strips grants it doesn't
# know about.

locals {
  sa_ids = {
    backend  = { id = "fde-support-backend-sa", name = "FDE Support Backend (Cloud Run runtime)" }
    frontend = { id = "fde-support-frontend-sa", name = "FDE Support Frontend (Cloud Run runtime)" }
    ci_eval  = { id = "fde-support-ci-eval-sa", name = "FDE Support CI eval (GitHub Actions via WIF)" }
  }

  project_number  = data.google_project.current.number
  compute_sa      = "${local.project_number}-compute@developer.gserviceaccount.com"
  cloudbuild_sa   = "${local.project_number}@cloudbuild.gserviceaccount.com"
  iap_service_agt = "service-${local.project_number}@gcp-sa-iap.iam.gserviceaccount.com"

  backend_roles = toset([
    "roles/aiplatform.user",
    "roles/cloudsql.client",
    "roles/cloudsql.instanceUser",
    "roles/cloudtrace.agent",
    "roles/discoveryengine.viewer",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])
}

resource "google_service_account" "sa" {
  for_each     = local.sa_ids
  project      = var.project_id
  account_id   = each.value.id
  display_name = each.value.name

  depends_on = [google_project_service.extra]

  lifecycle {
    ignore_changes = [display_name] # adopted SAs keep their existing names
  }
}

resource "google_project_iam_member" "backend" {
  for_each = local.backend_roles
  project  = var.project_id
  role     = each.value
  member   = google_service_account.sa["backend"].member
}

# Secret access is scoped to the one secret, not the project.
resource "google_secret_manager_secret_iam_member" "backend_db_secret" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.db.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.sa["backend"].member
}

# Existing + CI/CD bindings for the CI eval & deploy SA (GitHub Actions via WIF).
resource "google_project_iam_member" "ci_eval" {
  for_each = toset([
    "roles/aiplatform.admin",
    "roles/aiplatform.user",
    "roles/artifactregistry.writer",
    "roles/cloudbuild.builds.editor",
    "roles/discoveryengine.viewer",
    "roles/iam.serviceAccountUser",
    "roles/run.admin",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/storage.admin",
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.sa["ci_eval"].member
}

# Allow CI SA to act as the Cloud Run runtime service accounts when rolling revisions.
resource "google_service_account_iam_member" "ci_eval_act_as_runtime_sa" {
  for_each = toset(["backend", "frontend"])

  service_account_id = google_service_account.sa[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.sa["ci_eval"].member
}

# Allow CI SA to mint OIDC ID tokens for itself (used by Gate 5 Canary E2E smoke test).
resource "google_service_account_iam_member" "ci_eval_self_token_creator" {
  service_account_id = google_service_account.sa["ci_eval"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_service_account.sa["ci_eval"].member
}

# Cloud Build / default compute SA: OTel traces from build-time smoke tests (existing grant).
resource "google_project_iam_member" "compute_sa_trace" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${local.compute_sa}"
}
