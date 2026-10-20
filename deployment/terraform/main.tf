# Main Terraform Configuration for Support Ticket Router & Assistant

resource "google_project_service" "cloud_run" {
  project            = var.project_id
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "cloud_sql" {
  project            = var.project_id
  service            = "sqladmin.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "discovery_engine" {
  project            = var.project_id
  service            = "discoveryengine.googleapis.com"
  disable_on_destroy = false
}

# Cloud Storage Bucket for Agent Search Datastore Document Ingestion
resource "google_storage_bucket" "faq_docs" {
  name                        = "fde-support-faq-docs-${var.project_id}"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  versioning {
    enabled = true
  }
}

# Vertex AI Agent Search / Discovery Engine Data Store
resource "google_discovery_engine_data_store" "faq_datastore" {
  project                     = var.project_id
  location                    = "global"
  data_store_id               = "fde-support-faq-datastore-v2"
  display_name                = "Customer Support FAQ Knowledge Base V2"
  industry_vertical           = "GENERIC"
  content_config              = "NO_CONTENT"
  solution_types              = ["SOLUTION_TYPE_SEARCH"]
  create_advanced_site_search = false

  depends_on = [google_project_service.discovery_engine]
}

# Cloud SQL PostgreSQL Sandbox Instance
resource "google_sql_database_instance" "sandbox_db" {
  name             = "fde-support-db-instance"
  project          = var.project_id
  region           = var.region
  database_version = "POSTGRES_15"

  settings {
    # TDD: "Cloud SQL (PostgreSQL, 1 vCPU, 3.75 GB RAM sandbox instance)".
    tier    = var.db_tier
    edition = "ENTERPRISE"
    # Live instance was switched to HA in the console; kept (set ZONAL to halve cost).
    availability_type           = "REGIONAL"
    deletion_protection_enabled = true

    maintenance_window {
      day          = 7
      hour         = 3
      update_track = "stable"
    }

    password_validation_policy {
      enable_password_policy      = true
      min_length                  = 6
      disallow_username_substring = false
      reuse_interval              = 0
    }

    ip_configuration {
      # Public IP has no authorized networks: only the Cloud SQL Auth Proxy / Cloud Run
      # connector (IAM-authenticated) can reach it. Private IP lives in the sandbox VPC.
      ipv4_enabled                                  = var.enable_public_ip
      private_network                               = google_compute_network.sandbox.id
      enable_private_path_for_google_cloud_services = true
      ssl_mode                                      = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00"
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = 7
      }
    }

    insights_config {
      query_insights_enabled  = true
      record_application_tags = true
      record_client_address   = false
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    # Audit trail for database access (TDD "Observability & Audit").
    database_flags {
      name  = "log_connections"
      value = "on"
    }
    database_flags {
      name  = "log_disconnections"
      value = "on"
    }
    database_flags {
      name  = "log_checkpoints"
      value = "on"
    }
    database_flags {
      name  = "log_lock_waits"
      value = "on"
    }
  }

  deletion_protection = true

  depends_on = [google_service_networking_connection.psa]
}

resource "google_sql_database" "support_tickets" {
  name     = "support_tickets"
  instance = google_sql_database_instance.sandbox_db.name
  project  = var.project_id
}

output "cloud_sql_connection_name" {
  value       = google_sql_database_instance.sandbox_db.connection_name
  description = "Connection name for Cloud SQL PostgreSQL instance"
}

output "agent_search_datastore_id" {
  value       = google_discovery_engine_data_store.faq_datastore.data_store_id
  description = "Data Store ID for Vertex AI Agent Search RAG"
}

output "faq_docs_gcs_bucket" {
  value       = google_storage_bucket.faq_docs.name
  description = "GCS Bucket URI for storing and importing FAQ documents into Agent Search Data Store"
}
