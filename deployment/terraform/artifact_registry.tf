# Artifact Registry Repository for Container Images

resource "google_project_service" "artifact_registry" {
  project            = var.project_id
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "fde_support_repo" {
  provider      = google-beta
  project       = var.project_id
  location      = var.region
  repository_id = "fde-support-repo"
  description   = "Docker container repository for FDE Support Ticket Router microservices (frontend, backend, agents)"
  format        = "DOCKER"

  depends_on = [google_project_service.artifact_registry]
}

output "artifact_registry_repo_url" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.fde_support_repo.repository_id}"
  description = "Artifact Registry Docker repository base URL"
}
