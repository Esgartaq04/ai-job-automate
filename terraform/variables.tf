variable "project_id" {
  type        = string
  description = "GCP project to deploy into."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "image" {
  type        = string
  description = "Artifact Registry image, e.g. us-central1-docker.pkg.dev/PROJECT/autoapply/app:TAG"
}

variable "db_tier" {
  type        = string
  default     = "db-custom-1-3840"
  description = "Cloud SQL machine type. The smallest tier is enough for a single user."
}

variable "vpc_self_link" {
  type        = string
  default     = null
  description = "Self link of the VPC for private Cloud SQL IP. Required before apply."
}

variable "worker_machine_type" {
  type        = string
  default     = "e2-small"
  description = "Playwright needs ~1GB of RAM for headless Chrome."
}

variable "worker_count" {
  type        = number
  default     = 1
  description = "Keep this at 1 for Phase 1: submission volume is deliberately low."
}
