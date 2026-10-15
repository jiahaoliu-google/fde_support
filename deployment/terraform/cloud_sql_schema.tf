# Cloud SQL Database Schema Initialization for tickets and messages tables

resource "google_storage_bucket_object" "schema_sql" {
  name   = "schema.sql"
  bucket = google_storage_bucket.faq_docs.name
  source = "${path.module}/../../database/schema.sql"
}

# Grant Cloud SQL Service Account objectViewer on the GCS bucket to import SQL
resource "google_storage_bucket_iam_member" "cloudsql_gcs_reader" {
  bucket = google_storage_bucket.faq_docs.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_sql_database_instance.sandbox_db.service_account_email_address}"
}
