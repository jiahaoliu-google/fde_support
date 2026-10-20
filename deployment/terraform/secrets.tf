# Database credentials (TDD: "Secret Manager stores the database credentials").
#
# The password is generated as an *ephemeral* value and sent through write-only arguments,
# so it is never persisted in Terraform state. Bump var.db_password_version to rotate:
# a new secret version is written, the Cloud SQL user is updated, and the backend Cloud Run
# service rolls a new revision pinned to the new secret version (see cloud_run.tf).

ephemeral "random_password" "db" {
  length           = 32
  special          = true
  override_special = "-_.~"
}

resource "google_secret_manager_secret" "db" {
  project   = var.project_id
  secret_id = "fde-support-db-credentials"

  replication {
    auto {}
  }

  depends_on = [google_project_service.extra]
}

resource "google_secret_manager_secret_version" "db" {
  secret                 = google_secret_manager_secret.db.id
  secret_data_wo         = ephemeral.random_password.db.result
  secret_data_wo_version = var.db_password_version
}

resource "google_sql_user" "postgres" {
  project             = var.project_id
  instance            = google_sql_database_instance.sandbox_db.name
  name                = "postgres"
  password_wo         = ephemeral.random_password.db.result
  password_wo_version = var.db_password_version
}
