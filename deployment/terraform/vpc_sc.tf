# VPC Service Controls (TDD "Data Protection & Compliance").
#
# Adopts the existing perimeter `fde_support_perimeter` and access level
# `fde_support_dev_access` (see imports.tf).
#
#   status (ENFORCED) - exactly today's live config while var.vpc_sc_enforce_hardened = false.
#   spec   (DRY-RUN)  - the hardened config. Violations are only logged, never blocked.
#
# Promotion: once dry-run violation logs are clean (scripts/setup/check_vpcsc_dryrun.sh),
# set vpc_sc_enforce_hardened = true and apply - the hardened config becomes enforced.

locals {
  policy_name    = "accessPolicies/${var.access_policy_id}"
  project_res    = "projects/${local.project_number}"
  dev_access_lvl = "${local.policy_name}/accessLevels/fde_support_dev_access"

  current_restricted = [
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "discoveryengine.googleapis.com",
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ]

  hardened_restricted = concat(local.current_restricted, [
    "bigquery.googleapis.com",
    "dlp.googleapis.com",
    "modelarmor.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  # Workloads that call into the perimeter from outside it (Cloud Build workers, GitHub
  # Actions via WIF impersonation, Agent Engine tenants) or that run inside it.
  automation_identities = [
    "serviceAccount:${local.cloudbuild_sa}",
    "serviceAccount:${local.compute_sa}",
    google_service_account.sa["backend"].member,
    google_service_account.sa["frontend"].member,
    google_service_account.sa["ci_eval"].member,
    "serviceAccount:service-${local.project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com",
  ]

  # Google-owned project Cloud Build reads from (Oct 18 egress violations: storage.buckets.get).
  cloudbuild_egress_project = "projects/586809574927"
}

resource "google_access_context_manager_access_level" "dev_access" {
  parent = local.policy_name
  name   = local.dev_access_lvl
  title  = "FDE Support Developer Access"

  basic {
    # AND (default): any IP *and* one of the developer accounts.
    conditions {
      ip_subnetworks = ["0.0.0.0/0"]
    }
    conditions {
      members = var.developer_members
    }
  }
}

resource "google_access_context_manager_service_perimeter" "fde_support" {
  parent = local.policy_name
  name   = "${local.policy_name}/servicePerimeters/fde_support_perimeter"
  title  = "FDE Support Service Perimeter"

  use_explicit_dry_run_spec = true

  # ---------------------------------------------------------------- ENFORCED
  status {
    resources           = [local.project_res]
    restricted_services = var.vpc_sc_enforce_hardened ? local.hardened_restricted : local.current_restricted

    # Today's (permissive) rule - kept until the hardened rules are promoted.
    dynamic "ingress_policies" {
      for_each = var.vpc_sc_enforce_hardened ? [] : [1]
      content {
        title = "fde-support"
        ingress_from {
          identity_type = "ANY_IDENTITY"
          sources {
            access_level = "*"
          }
        }
        ingress_to {
          resources = [local.project_res]
          operations {
            service_name = "*"
          }
        }
      }
    }

    dynamic "ingress_policies" {
      for_each = var.vpc_sc_enforce_hardened ? [1] : []
      content {
        title = "developers-any-network"
        ingress_from {
          identities = var.developer_members
          sources {
            access_level = google_access_context_manager_access_level.dev_access.name
          }
        }
        ingress_to {
          resources = ["*"]
          operations {
            service_name = "*"
          }
        }
      }
    }

    dynamic "ingress_policies" {
      for_each = var.vpc_sc_enforce_hardened ? [1] : []
      content {
        title = "automation-service-accounts"
        ingress_from {
          identities = local.automation_identities
          sources {
            access_level = "*"
          }
        }
        ingress_to {
          resources = ["*"]
          operations {
            service_name = "*"
          }
        }
      }
    }

    dynamic "egress_policies" {
      for_each = var.vpc_sc_enforce_hardened ? [1] : []
      content {
        title = "cloudbuild-google-buckets"
        egress_from {
          identities = ["serviceAccount:${local.cloudbuild_sa}", "serviceAccount:${local.compute_sa}"]
        }
        egress_to {
          resources = [local.cloudbuild_egress_project]
          operations {
            service_name = "storage.googleapis.com"
            method_selectors { method = "google.storage.buckets.get" }
            method_selectors { method = "google.storage.objects.get" }
            method_selectors { method = "google.storage.objects.list" }
          }
        }
      }
    }
  }

  # ---------------------------------------------------------------- DRY-RUN
  spec {
    resources           = [local.project_res]
    restricted_services = local.hardened_restricted

    ingress_policies {
      title = "developers-any-network"
      ingress_from {
        identities = var.developer_members
        sources {
          access_level = google_access_context_manager_access_level.dev_access.name
        }
      }
      ingress_to {
        resources = ["*"]
        operations {
          service_name = "*"
        }
      }
    }

    ingress_policies {
      title = "automation-service-accounts"
      ingress_from {
        identities = local.automation_identities
        sources {
          access_level = "*"
        }
      }
      ingress_to {
        resources = ["*"]
        operations {
          service_name = "*"
        }
      }
    }

    egress_policies {
      title = "cloudbuild-google-buckets"
      egress_from {
        identities = ["serviceAccount:${local.cloudbuild_sa}", "serviceAccount:${local.compute_sa}"]
      }
      egress_to {
        resources = [local.cloudbuild_egress_project]
        operations {
          service_name = "storage.googleapis.com"
          method_selectors { method = "google.storage.buckets.get" }
          method_selectors { method = "google.storage.objects.get" }
          method_selectors { method = "google.storage.objects.list" }
        }
      }
    }
  }

  lifecycle {
    # Org-level object: never let a stray destroy remove the perimeter.
    prevent_destroy = true
  }
}

output "vpc_sc_mode" {
  value = var.vpc_sc_enforce_hardened ? "hardened rules ENFORCED" : "hardened rules in DRY-RUN; legacy rules enforced"
}
