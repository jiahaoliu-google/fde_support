# Cloud Run services (scoping doc Sprint 5: "deploy to Google Cloud Run").
#
# Hybrid image mode
#   * var.backend_image / var.frontend_image = null (default): Terraform keeps whatever image
#     is currently live, so `backend/deploy.sh` / `frontend/deploy.sh` can roll new images
#     without Terraform rolling them back.
#   * Set the variable (ideally to an @sha256 digest) to pin a release or roll back.
# A brand-new project has no live service, so the variables must be set on the first apply.

data "google_cloud_run_v2_service" "backend_live" {
  project  = var.project_id
  name     = "fde-support-backend"
  location = var.region
}

data "google_cloud_run_v2_service" "frontend_live" {
  project  = var.project_id
  name     = "fde-support-frontend"
  location = var.region
}

locals {
  backend_image = coalesce(
    var.backend_image,
    try(data.google_cloud_run_v2_service.backend_live.template[0].containers[0].image, null),
  )
  frontend_image = coalesce(
    var.frontend_image,
    try(data.google_cloud_run_v2_service.frontend_live.template[0].containers[0].image, null),
  )

  # Audience of the IAP-signed header, derived from the IAP backend service (was hard-coded).
  iap_audience = "/projects/${local.project_number}/global/backendServices/${google_compute_backend_service.frontend.generated_id}"
}

# -----------------------------------------------------------------------------
# Backend (FastAPI)
# -----------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "backend" {
  project  = var.project_id
  name     = "fde-support-backend"
  location = var.region

  # ALL is required: the frontend calls the run.app URL over the internet, and developers
  # test it from their terminal. Every request still needs an IAM-authorized ID token
  # (no allUsers invoker), and VPC-SC governs the API plane.
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true

  template {
    service_account                  = google_service_account.sa["backend"].email
    timeout                          = "300s"
    max_instance_request_concurrency = 80

    scaling {
      max_instance_count = 100
    }

    # Direct VPC egress: private ranges (Cloud SQL private IP) go through the sandbox VPC.
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.sandbox.id
        subnetwork = google_compute_subnetwork.sandbox.id
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.sandbox_db.connection_name]
      }
    }

    containers {
      image = local.backend_image

      ports {
        name           = "http1"
        container_port = 8000
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
        startup_cpu_boost = true
        cpu_idle          = true
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      dynamic "env" {
        for_each = {
          ENV                                                = "production"
          GCP_PROJECT_ID                                     = var.project_id
          CLOUD_SQL_CONNECTION_NAME                          = google_sql_database_instance.sandbox_db.connection_name
          GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY         = "true"
          OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT = "SPAN_AND_EVENT"
          OTEL_SERVICE_NAME                                  = "fde-support-backend"
          OTEL_RESOURCE_ATTRIBUTES                           = "service.name=fde-support-backend"
          FAQ_AGENT_URL                                      = var.faq_agent_url
          SENTIMENT_AGENT_URL                                = var.sentiment_agent_url
          IAP_AUDIENCE                                       = local.iap_audience
          FRONTEND_SERVICE_ACCOUNT                           = google_service_account.sa["frontend"].email
          ALLOW_DIRECT_CALLERS                               = tostring(var.allow_direct_callers)
          MODEL_ARMOR_TEMPLATE_ID                            = google_model_armor_template.pii_redaction.template_id
          MODEL_ARMOR_LOCATION                               = var.region
        }
        content {
          name  = env.key
          value = env.value
        }
      }

      # Pinned to the version Terraform wrote: rotating the password rolls a new revision.
      env {
        name = "DB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db.secret_id
            version = google_secret_manager_secret_version.db.version
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }

  depends_on = [
    google_project_service.cloud_run,
    google_secret_manager_secret_iam_member.backend_db_secret,
    google_sql_user.postgres,
    # Redaction chain (backend/security/model_armor.py) needs these before the revision serves.
    google_project_iam_member.model_armor_users,
    google_project_iam_member.backend_dlp_user,
  ]
}

# Only the frontend SA (plus explicit test identities) may invoke the backend.
resource "google_cloud_run_v2_service_iam_member" "backend_invokers" {
  for_each = toset(concat([google_service_account.sa["frontend"].member], var.backend_invokers))
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.backend.name
  role     = "roles/run.invoker"
  member   = each.value
}

# -----------------------------------------------------------------------------
# Frontend
# -----------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "frontend" {
  project             = var.project_id
  name                = "fde-support-frontend"
  location            = var.region
  ingress             = var.frontend_ingress
  deletion_protection = true

  template {
    service_account                  = google_service_account.sa["frontend"].email
    timeout                          = "300s"
    max_instance_request_concurrency = 80

    scaling {
      max_instance_count = 100
    }

    containers {
      image = local.frontend_image

      ports {
        name           = "http1"
        container_port = 3000
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
        startup_cpu_boost = true
        cpu_idle          = true
      }

      env {
        name  = "CLOUD_RUN_BACKEND_URL"
        value = google_cloud_run_v2_service.backend.uri
      }
      env {
        name  = "IAP_AUDIENCE"
        value = local.iap_audience
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version, build_config]
  }

  depends_on = [google_project_service.cloud_run]
}

resource "google_cloud_run_v2_service_iam_member" "frontend_invokers" {
  for_each = toset(concat(["serviceAccount:${local.iap_service_agt}"], var.frontend_invokers))
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.frontend.name
  role     = "roles/run.invoker"
  member   = each.value
}

output "backend_url" {
  value = google_cloud_run_v2_service.backend.uri
}

output "frontend_run_url" {
  value = google_cloud_run_v2_service.frontend.uri
}

output "iap_audience" {
  value = local.iap_audience
}
