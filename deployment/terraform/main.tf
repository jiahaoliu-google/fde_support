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
  data_store_id               = "fde-support-faq-datastore"
  display_name                = "Customer Support FAQ Knowledge Base"
  industry_vertical           = "GENERIC"
  content_config              = "CONTENT_REQUIRED"
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
    tier = "db-f1-micro"
    ip_configuration {
      ipv4_enabled = true
    }
  }

  deletion_protection = false
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
