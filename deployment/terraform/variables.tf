variable "project_id" {
  type        = string
  description = "The GCP project ID to deploy resources into."
  default     = "fde-support-sandbox-dev-jliu"
}

variable "region" {
  type        = string
  description = "The GCP region for resource deployment."
  default     = "us-central1"
}

variable "zone" {
  type        = string
  description = "The default GCP availability zone."
  default     = "us-central1-a"
}
