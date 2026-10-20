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

# -----------------------------------------------------------------------------
# Cloud SQL
# -----------------------------------------------------------------------------
variable "db_tier" {
  type        = string
  description = "Cloud SQL machine tier. TDD: 1 vCPU / 3.75 GB sandbox instance."
  default     = "db-custom-1-3840"
}

variable "enable_public_ip" {
  type        = bool
  description = "Keep the Cloud SQL public IP (no authorized networks: reachable only via Cloud SQL Auth Proxy / connector with IAM). Set false once local dev uses --private-ip."
  default     = true
}

variable "db_password_version" {
  type        = number
  description = "Bump to rotate the DB password. The password is write-only: it is stored in Secret Manager and Cloud SQL but never in Terraform state."
  default     = 1
}

# -----------------------------------------------------------------------------
# Network
# -----------------------------------------------------------------------------
variable "subnet_cidr" {
  type        = string
  description = "Primary range of the sandbox subnet (Cloud Run Direct VPC egress)."
  default     = "10.10.0.0/24"
}

variable "psa_prefix_length" {
  type        = number
  description = "Prefix length of the range reserved for private service access (Cloud SQL private IP)."
  default     = 20
}

# -----------------------------------------------------------------------------
# Cloud Run (hybrid image mode)
# -----------------------------------------------------------------------------
variable "backend_image" {
  type        = string
  description = "Pinned backend image (prefer @sha256 digest). null = keep whatever image is live (deploy.sh rolls images)."
  default     = null
}

variable "frontend_image" {
  type        = string
  description = "Pinned frontend image (prefer @sha256 digest). null = keep whatever image is live."
  default     = null
}

variable "faq_agent_url" {
  type        = string
  description = "Agent Engine streamQuery URL of the FAQ agent (deployed by ADK, not Terraform)."
  default     = "https://us-central1-aiplatform.googleapis.com/v1/projects/883790454740/locations/us-central1/reasoningEngines/6767283237606653952:streamQuery"
}

variable "sentiment_agent_url" {
  type        = string
  description = "Agent Engine streamQuery URL of the sentiment router agent (deployed by ADK, not Terraform)."
  default     = "https://us-central1-aiplatform.googleapis.com/v1/projects/883790454740/locations/us-central1/reasoningEngines/7184992103045267456:streamQuery"
}

variable "allow_direct_callers" {
  type        = bool
  description = "Testing: principals with run.invoker may call the backend URL directly, acting as themselves."
  default     = true
}

variable "frontend_ingress" {
  type        = string
  description = "Frontend Cloud Run ingress. INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER forces all users through IAP; ALL keeps the run.app URL reachable for invokers."
  default     = "INGRESS_TRAFFIC_ALL"
}

variable "backend_invokers" {
  type        = list(string)
  description = "Extra principals allowed to invoke the backend directly (the frontend SA is always granted)."
  default     = ["serviceAccount:883790454740-compute@developer.gserviceaccount.com"]
}

variable "frontend_invokers" {
  type        = list(string)
  description = "Extra principals allowed to invoke the frontend run.app URL (the IAP service agent is always granted)."
  default = [
    "domain:google.com",
    "user:jiahaoliu@google.com",
    "serviceAccount:883790454740-compute@developer.gserviceaccount.com",
    "serviceAccount:service-883790454740@compute-system.iam.gserviceaccount.com",
  ]
}

# -----------------------------------------------------------------------------
# IAP / Load balancer
# -----------------------------------------------------------------------------
variable "iap_members" {
  type        = list(string)
  description = "Principals granted IAP-secured Web App User on the frontend. Empty = don't manage the binding."
  default     = []
}

variable "lb_domain" {
  type        = string
  description = "Domain on the managed certificate. Empty = <lb-ip>.nip.io."
  default     = ""
}

# -----------------------------------------------------------------------------
# VPC Service Controls
# -----------------------------------------------------------------------------
variable "access_policy_id" {
  type        = string
  description = "Access Context Manager policy number (org-level)."
  default     = "225119263746"
}

variable "developer_members" {
  type        = list(string)
  description = "Humans allowed through the perimeter from any network (terminal deploys and tests)."
  default = [
    "user:admin@jiahaoliu.altostrat.com",
    "user:jiahaoliu@google.com",
  ]
}

variable "vpc_sc_enforce_hardened" {
  type        = bool
  description = "false = hardened rules live only in the dry-run spec (enforced config unchanged). true = promote the hardened rules to enforced."
  default     = false
}
