# Adopt resources that were created by gcloud scripts or in the console.
# Safe to keep after the first apply (import blocks are no-ops once the address is in state).

locals {
  p = var.project_id
}

# --- Agent Search datastore (indexed corpus; must never be recreated) --------
import {
  to = google_discovery_engine_data_store.faq_datastore
  id = "projects/${local.p}/locations/global/collections/default_collection/dataStores/fde-support-faq-datastore-v2"
}

# --- Service accounts ---------------------------------------------------------
import {
  for_each = local.sa_ids
  to       = google_service_account.sa[each.key]
  id       = "projects/${local.p}/serviceAccounts/${each.value.id}@${local.p}.iam.gserviceaccount.com"
}

# --- Secret / DB user ---------------------------------------------------------
import {
  to = google_secret_manager_secret.db
  id = "projects/${local.p}/secrets/fde-support-db-credentials"
}

import {
  to = google_sql_user.postgres
  id = "${local.p}/fde-support-db-instance/postgres"
}

# --- Cloud Run ----------------------------------------------------------------
import {
  to = google_cloud_run_v2_service.backend
  id = "projects/${local.p}/locations/${var.region}/services/fde-support-backend"
}

import {
  to = google_cloud_run_v2_service.frontend
  id = "projects/${local.p}/locations/${var.region}/services/fde-support-frontend"
}

# --- Load balancer / IAP ------------------------------------------------------
import {
  to = google_compute_global_address.frontend
  id = "projects/${local.p}/global/addresses/fde-frontend-ip"
}

import {
  to = google_compute_region_network_endpoint_group.frontend
  id = "projects/${local.p}/regions/${var.region}/networkEndpointGroups/fde-frontend-neg"
}

import {
  to = google_compute_backend_service.frontend
  id = "projects/${local.p}/global/backendServices/fde-frontend-backend-service"
}

import {
  to = google_compute_url_map.frontend
  id = "projects/${local.p}/global/urlMaps/fde-frontend-url-map"
}

import {
  to = google_compute_managed_ssl_certificate.frontend
  id = "projects/${local.p}/global/sslCertificates/fde-frontend-managed-cert"
}

import {
  to = google_compute_target_https_proxy.frontend
  id = "projects/${local.p}/global/targetHttpsProxies/fde-frontend-https-proxy"
}

import {
  to = google_compute_target_http_proxy.frontend
  id = "projects/${local.p}/global/targetHttpProxies/fde-frontend-http-proxy"
}

import {
  to = google_compute_global_forwarding_rule.frontend_https
  id = "projects/${local.p}/global/forwardingRules/fde-frontend-https-forwarding-rule"
}

import {
  to = google_compute_global_forwarding_rule.frontend_http
  id = "projects/${local.p}/global/forwardingRules/fde-frontend-forwarding-rule"
}

# --- VPC Service Controls -----------------------------------------------------
import {
  to = google_access_context_manager_access_level.dev_access
  id = "accessPolicies/${var.access_policy_id}/accessLevels/fde_support_dev_access"
}

import {
  to = google_access_context_manager_service_perimeter.fde_support
  id = "accessPolicies/${var.access_policy_id}/servicePerimeters/fde_support_perimeter"
}
