# Identity-Aware Proxy (IAP) & external HTTPS load balancer in front of the frontend
# (TDD "User Authentication": protect public endpoints using IAP).
#
# These resources were first created by hand; imports.tf adopts them unchanged.

resource "google_project_service" "iap" {
  project            = var.project_id
  service            = "iap.googleapis.com"
  disable_on_destroy = false
}

resource "google_compute_global_address" "frontend" {
  project = var.project_id
  name    = "fde-frontend-ip"
}

locals {
  lb_domain = var.lb_domain != "" ? var.lb_domain : "${google_compute_global_address.frontend.address}.nip.io"
}

resource "google_compute_region_network_endpoint_group" "frontend" {
  project               = var.project_id
  name                  = "fde-frontend-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    # Literal name (not a reference): the backend's IAP_AUDIENCE depends on this NEG's
    # backend service, so referencing the frontend resource would form a cycle.
    service = "fde-support-frontend"
  }
}

resource "google_compute_backend_service" "frontend" {
  project                         = var.project_id
  name                            = "fde-frontend-backend-service"
  protocol                        = "HTTP"
  port_name                       = "http"
  timeout_sec                     = 30
  load_balancing_scheme           = "EXTERNAL"
  enable_cdn                      = false
  connection_draining_timeout_sec = 0

  backend {
    group           = google_compute_region_network_endpoint_group.frontend.id
    balancing_mode  = "UTILIZATION"
    capacity_scaler = 1.0
  }

  iap {
    enabled = true
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }

  lifecycle {
    # The custom OAuth client was configured out of band; its secret can't be read back.
    ignore_changes = [iap[0].oauth2_client_id, iap[0].oauth2_client_secret]
  }

  depends_on = [google_project_service.iap]
}

resource "google_compute_url_map" "frontend" {
  project         = var.project_id
  name            = "fde-frontend-url-map"
  default_service = google_compute_backend_service.frontend.id
}

resource "google_compute_managed_ssl_certificate" "frontend" {
  project = var.project_id
  name    = "fde-frontend-managed-cert"

  managed {
    domains = [local.lb_domain]
  }
}

resource "google_compute_target_https_proxy" "frontend" {
  project          = var.project_id
  name             = "fde-frontend-https-proxy"
  url_map          = google_compute_url_map.frontend.id
  ssl_certificates = [google_compute_managed_ssl_certificate.frontend.id]
}

# Port 80 currently serves the same URL map (IAP still enforces login).
resource "google_compute_target_http_proxy" "frontend" {
  project = var.project_id
  name    = "fde-frontend-http-proxy"
  url_map = google_compute_url_map.frontend.id
}

resource "google_compute_global_forwarding_rule" "frontend_https" {
  project               = var.project_id
  name                  = "fde-frontend-https-forwarding-rule"
  ip_address            = google_compute_global_address.frontend.address
  port_range            = "443-443"
  target                = google_compute_target_https_proxy.frontend.id
  load_balancing_scheme = "EXTERNAL"
}

resource "google_compute_global_forwarding_rule" "frontend_http" {
  project               = var.project_id
  name                  = "fde-frontend-forwarding-rule"
  ip_address            = google_compute_global_address.frontend.address
  port_range            = "80-80"
  target                = google_compute_target_http_proxy.frontend.id
  load_balancing_scheme = "EXTERNAL"
}

# Who may pass IAP. Empty list = binding not managed by Terraform.
resource "google_iap_web_backend_service_iam_member" "users" {
  for_each            = toset(var.iap_members)
  project             = var.project_id
  web_backend_service = google_compute_backend_service.frontend.name
  role                = "roles/iap.httpsResourceAccessor"
  member              = each.value
}

output "iap_enabled" {
  description = "Indicates whether Identity-Aware Proxy is enabled"
  value       = true
}

output "frontend_url" {
  description = "IAP-protected frontend URL"
  value       = "https://${local.lb_domain}"
}
