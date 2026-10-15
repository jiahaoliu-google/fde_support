# Cloud Build Service API & IAM permissions

resource "google_project_service" "cloud_build" {
  project            = var.project_id
  service            = "cloudbuild.googleapis.com"
  disable_on_destroy = false
}

# Grant Default Compute Service Account Storage Object Viewer for Cloud Build source archives
resource "google_project_iam_member" "compute_sa_storage_viewer" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:883790454740-compute@developer.gserviceaccount.com"
}

# Grant Default Compute Service Account Artifact Registry Writer
resource "google_project_iam_member" "compute_sa_ar_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:883790454740-compute@developer.gserviceaccount.com"
}

# Grant Default Compute Service Account Logs Writer
resource "google_project_iam_member" "compute_sa_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:883790454740-compute@developer.gserviceaccount.com"
}

# Grant Cloud Build Service Account Artifact Registry Writer
resource "google_project_iam_member" "cloudbuild_sa_ar_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:883790454740@cloudbuild.gserviceaccount.com"
}

# Grant Cloud Build Service Account Storage Admin for source staging
resource "google_project_iam_member" "cloudbuild_sa_storage_admin" {
  project = var.project_id
  role    = "roles/storage.admin"
  member  = "serviceAccount:883790454740@cloudbuild.gserviceaccount.com"
}
