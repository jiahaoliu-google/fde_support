# Agent Gateway and Model Armor Governance Configuration

resource "google_project_service" "agent_gateway" {
  project            = var.project_id
  service            = "networkservices.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "model_armor" {
  project            = var.project_id
  service            = "modelarmor.googleapis.com"
  disable_on_destroy = false
}

# Model Armor Template for PII Redaction and Governance
resource "google_model_armor_template" "pii_redaction" {
  project     = var.project_id
  location    = var.region
  template_id = "support-router-pii-masking"

  filter_config {
    sdp_settings {
      basic_config {
        filter_enforcement = "ENABLED"
      }
    }
  }

  depends_on = [google_project_service.model_armor]
}

output "model_armor_template_name" {
  description = "Resource name of the Model Armor PII Redaction template"
  value       = google_model_armor_template.pii_redaction.name
}
