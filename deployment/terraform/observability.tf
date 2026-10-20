# Observability & audit (TDD: BigQuery telemetry sink, structured logging, audit trails,
# circuit-breaker monitoring). OpenTelemetry -> Cloud Trace is configured in the app; the
# backend SA's cloudtrace.agent role is in iam.tf.

resource "google_bigquery_dataset" "telemetry" {
  project       = var.project_id
  dataset_id    = "support_telemetry"
  friendly_name = "Support router telemetry"
  description   = "Backend structured logs streamed from Cloud Logging (Looker / KPI dashboards)."
  location      = "US"

  default_partition_expiration_ms = 90 * 24 * 60 * 60 * 1000 # 90 days

  depends_on = [google_project_service.extra]
}

resource "google_logging_project_sink" "backend_to_bq" {
  project     = var.project_id
  name        = "fde-support-backend-to-bq"
  destination = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.telemetry.dataset_id}"
  filter      = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=(\"fde-support-backend\" OR \"fde-support-frontend\")"

  unique_writer_identity = true

  bigquery_options {
    use_partitioned_tables = true
  }
}

resource "google_bigquery_dataset_iam_member" "sink_writer" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.telemetry.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.backend_to_bq.writer_identity
}

# Audit trails: Secret Manager reads and Cloud SQL data access.
resource "google_project_iam_audit_config" "data_access" {
  for_each = toset(["secretmanager.googleapis.com", "cloudsql.googleapis.com"])
  project  = var.project_id
  service  = each.value

  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

# Circuit breaker trips (backend/core/circuit_breaker.py logs "tripping to OPEN state").
resource "google_logging_metric" "circuit_breaker_open" {
  project = var.project_id
  name    = "fde_support/circuit_breaker_open"
  filter  = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"fde-support-backend\" AND (textPayload:\"Circuit breaker tripping to OPEN\" OR jsonPayload.message:\"Circuit breaker tripping to OPEN\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "circuit_breaker_open" {
  project      = var.project_id
  display_name = "FDE Support: circuit breaker OPEN"
  combiner     = "OR"

  conditions {
    display_name = "Circuit breaker tripped"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.circuit_breaker_open.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  documentation {
    content   = "Agent Search / FAQ agent returned 3 consecutive 429/503s; FAQ traffic is falling back to the human queue."
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.extra]
}

output "telemetry_dataset" {
  value = google_bigquery_dataset.telemetry.dataset_id
}
