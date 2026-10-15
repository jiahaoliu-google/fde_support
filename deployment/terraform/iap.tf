# Identity-Aware Proxy (IAP) & Google Cloud Identity Terraform Configuration

resource "google_project_service" "iap" {
  project            = var.project_id
  service            = "iap.googleapis.com"
  disable_on_destroy = false
}

output "iap_enabled" {
  description = "Indicates whether Identity-Aware Proxy is enabled"
  value       = true
}
