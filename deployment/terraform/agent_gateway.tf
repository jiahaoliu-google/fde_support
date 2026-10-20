# Agent Gateway and Model Armor Governance Configuration
#
# The backend calls Model Armor sanitizeUserPrompt on every customer message
# (backend/security/model_armor.py) with the template below. One call:
#   * de-identifies PII with the SDP inspect + de-identify templates (advanced SDP config;
#     a basic config only detects PII and never returns rewritten text),
#   * screens for prompt injection / jailbreak and malicious URIs (backend: human queue,
#     no LLM call),
#   * runs Responsible AI filters (backend: urgent, human queue).

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

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  # Same tokens as INFO_TYPE_TOKENS in backend/security/model_armor.py and its regex layer,
  # so stored tickets look the same whichever layer redacted them.
  pii_tokens = {
    CREDIT_CARD_NUMBER        = "[CREDIT_CARD_REDACTED]"
    US_SOCIAL_SECURITY_NUMBER = "[SSN_REDACTED]"
    EMAIL_ADDRESS             = "[EMAIL_REDACTED]"
    PHONE_NUMBER              = "[PHONE_REDACTED]"
    PERSON_NAME               = "[NAME_REDACTED]"
  }
  dlp_parent = "projects/${var.project_id}/locations/${var.region}"
}

# -----------------------------------------------------------------------------
# Sensitive Data Protection templates used by the Model Armor template.
# Must be in the same region as the Model Armor template.
# -----------------------------------------------------------------------------
resource "google_data_loss_prevention_inspect_template" "support_pii" {
  parent       = local.dlp_parent
  template_id  = "support-pii-inspect"
  display_name = "Support router PII inspect"
  description  = "PII found in customer support messages before they reach Gemini."

  inspect_config {
    dynamic "info_types" {
      for_each = keys(local.pii_tokens)
      content {
        name = info_types.value
      }
    }
    # LIKELY: name detection has false positives on brand / store / product names, which
    # hurt FAQ retrieval. Cards, SSNs, emails and phone numbers below this threshold are
    # still caught by the backend's always-on regex layer.
    min_likelihood = "LIKELY"
  }

  depends_on = [google_project_service.dlp]
}

resource "google_data_loss_prevention_deidentify_template" "support_pii" {
  parent       = local.dlp_parent
  template_id  = "support-pii-deidentify"
  display_name = "Support router PII de-identify"
  description  = "Replaces each PII infoType with the backend's redaction token."

  deidentify_config {
    info_type_transformations {
      dynamic "transformations" {
        for_each = local.pii_tokens
        content {
          info_types {
            name = transformations.key
          }
          primitive_transformation {
            replace_config {
              new_value {
                string_value = transformations.value
              }
            }
          }
        }
      }
    }
  }

  depends_on = [google_project_service.dlp]
}

# Model Armor Template for PII Redaction and Governance
resource "google_model_armor_template" "pii_redaction" {
  project     = var.project_id
  location    = var.region
  template_id = "support-router-pii-masking"

  filter_config {
    sdp_settings {
      advanced_config {
        inspect_template    = google_data_loss_prevention_inspect_template.support_pii.id
        deidentify_template = google_data_loss_prevention_deidentify_template.support_pii.id
      }
    }

    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }

    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }

    # HIGH only: an RAI match escalates the ticket to a human (never blocks the customer),
    # and frustrated-but-legitimate customers should still get the FAQ assistant.
    rai_settings {
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "HIGH"
      }
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "HIGH"
      }
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "HIGH"
      }
      rai_filters {
        filter_type      = "SEXUALLY_EXPLICIT"
        confidence_level = "HIGH"
      }
    }
  }

  template_metadata {
    # Don't copy raw customer prompts into Cloud Logging.
    log_sanitize_operations = false
    log_template_operations = true
  }

  depends_on = [
    google_project_service.model_armor,
    google_project_iam_member.model_armor_agent_dlp,
  ]
}

# -----------------------------------------------------------------------------
# IAM
# -----------------------------------------------------------------------------
resource "google_project_service_identity" "model_armor" {
  provider = google-beta
  project  = var.project_id
  service  = "modelarmor.googleapis.com"

  depends_on = [google_project_service.model_armor]
}

# The Model Armor service agent reads and runs the SDP templates.
resource "google_project_iam_member" "model_armor_agent_dlp" {
  for_each = toset(["roles/dlp.user", "roles/dlp.reader"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-modelarmor.iam.gserviceaccount.com"

  depends_on = [google_project_service_identity.model_armor]
}

# Callers of sanitizeUserPrompt: the backend runtime SA and the CI eval SA (the
# pre-deployment eval measures the real screening path).
resource "google_project_iam_member" "model_armor_users" {
  for_each = toset([
    "serviceAccount:fde-support-backend-sa@${var.project_id}.iam.gserviceaccount.com",
    "serviceAccount:fde-support-ci-eval-sa@${var.project_id}.iam.gserviceaccount.com",
  ])
  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = each.value

  depends_on = [google_project_service.model_armor]
}

output "model_armor_template_name" {
  description = "Resource name of the Model Armor PII Redaction template"
  value       = google_model_armor_template.pii_redaction.name
}
