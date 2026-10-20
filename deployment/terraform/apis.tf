# Additional APIs required by the TDD (existing ones stay in main.tf / agent_gateway.tf /
# dlp.tf / iap.tf / artifact_registry.tf / cloud_build.tf so their state addresses don't change).

locals {
  extra_services = toset([
    "accesscontextmanager.googleapis.com",
    "aiplatform.googleapis.com",
    "bigquery.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
  ])
}

resource "google_project_service" "extra" {
  for_each           = local.extra_services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
