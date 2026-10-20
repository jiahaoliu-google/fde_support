# Cloud DLP (Sensitive Data Protection) for PII redaction in the backend
# (backend/security/model_armor.py: deidentify_content on every customer message).

resource "google_project_service" "dlp" {
  project            = var.project_id
  service            = "dlp.googleapis.com"
  disable_on_destroy = false
}

# Backend Cloud Run runtime service account: calls deidentify_content.
resource "google_project_iam_member" "backend_dlp_user" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:fde-support-backend-sa@${var.project_id}.iam.gserviceaccount.com"

  depends_on = [google_project_service.dlp]
}

# CI eval service account (GitHub Actions via WIF): the pre-deployment eval measures the
# real DLP redaction path.
resource "google_project_iam_member" "ci_eval_dlp_user" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:fde-support-ci-eval-sa@${var.project_id}.iam.gserviceaccount.com"

  depends_on = [google_project_service.dlp]
}
