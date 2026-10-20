terraform {
  # >= 1.11: write-only arguments (DB password never lands in state); import {} blocks.
  required_version = ">= 1.11.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.50.0, < 7.0.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 6.50.0, < 7.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.7.0"
    }
  }

  # Remote state. The bucket is bootstrapped once by scripts/setup/bootstrap_tfstate.sh
  # (a backend bucket can't be managed by the configuration that uses it).
  backend "gcs" {
    bucket = "fde-support-tfstate-fde-support-sandbox-dev-jliu"
    prefix = "fde-support/sandbox"
  }
}

provider "google" {
  project               = var.project_id
  region                = var.region
  zone                  = var.zone
  user_project_override = true
  billing_project       = var.project_id
}

provider "google-beta" {
  project               = var.project_id
  region                = var.region
  zone                  = var.zone
  user_project_override = true
  billing_project       = var.project_id
}
