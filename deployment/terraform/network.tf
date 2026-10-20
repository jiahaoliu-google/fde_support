# Sandbox VPC (TDD: "Cloud SQL provisioned within the Sandbox VPC").

resource "google_compute_network" "sandbox" {
  project                 = var.project_id
  name                    = "fde-support-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.extra]
}

resource "google_compute_subnetwork" "sandbox" {
  project                  = var.project_id
  name                     = "fde-support-${var.region}"
  region                   = var.region
  network                  = google_compute_network.sandbox.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# Private service access: Cloud SQL gets a private IP inside this VPC.
resource "google_compute_global_address" "psa_range" {
  project       = var.project_id
  name          = "fde-support-psa-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.psa_prefix_length
  network       = google_compute_network.sandbox.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.sandbox.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa_range.name]
}

# Default-deny ingress (explicit, auditable). Egress stays default-allow inside the VPC.
resource "google_compute_firewall" "deny_all_ingress" {
  project   = var.project_id
  name      = "fde-support-deny-all-ingress"
  network   = google_compute_network.sandbox.id
  direction = "INGRESS"
  priority  = 65534

  deny {
    protocol = "all"
  }
  source_ranges = ["0.0.0.0/0"]

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

output "vpc_network" {
  value = google_compute_network.sandbox.name
}
