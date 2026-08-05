# AutoApply on GCP — README section 7.
#
# NOT APPLIED OR VALIDATED against a real project. Treat this as the reviewed
# starting point for `terraform plan`, not as known-good infrastructure: quotas,
# org policies, and the VPC connector in particular will need adjustment.
#
# The split follows the design doc: Cloud Run for the API, Cloud Run Jobs for the
# cheap stages, and a warm-start MIG for Playwright (headless Chrome needs ~1GB
# and does not tolerate Cloud Run cold starts well).

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.30"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  services = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudkms.googleapis.com",
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
    "cloudtasks.googleapis.com",
  ]
  db_url_secret = "autoapply-database-url"
}

resource "google_project_service" "enabled" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- identity

resource "google_service_account" "api" {
  account_id   = "autoapply-api"
  display_name = "AutoApply API"
}

resource "google_service_account" "worker" {
  account_id   = "autoapply-worker"
  display_name = "AutoApply workers"
}

resource "google_project_iam_member" "worker_sql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "api_sql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "worker_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "api_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.api.email}"
}

# ---------------------------------------------------------------- data

resource "google_sql_database_instance" "postgres" {
  name             = "autoapply-pg"
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_autoresize   = true
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }
    database_flags {
      # pgvector must be enabled per-database with CREATE EXTENSION after apply;
      # migrations/001_init.sql does that.
      name  = "cloudsql.enable_pgaudit"
      value = "on"
    }
    ip_configuration {
      ipv4_enabled = false
      # A private-IP instance needs a VPC peering range; wire private_network
      # to your VPC before applying.
      private_network = var.vpc_self_link
    }
  }

  deletion_protection = true
}

resource "google_sql_database" "autoapply" {
  name     = "autoapply"
  instance = google_sql_database_instance.postgres.name
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-autoapply-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true

  # Screenshots and DOM snapshots get big fast. README section 7.
  lifecycle_rule {
    condition {
      age                = 90
      matches_prefix     = ["screenshot/", "dom_snapshot/"]
    }
    action { type = "Delete" }
  }
}

resource "google_storage_bucket_iam_member" "worker_objects" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_storage_bucket_iam_member" "api_objects" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.api.email}"
}

# ---------------------------------------------------------------- secrets

# Envelope-encrypt user credentials. Never env vars for user credentials.
resource "google_kms_key_ring" "autoapply" {
  name     = "autoapply"
  location = var.region
}

resource "google_kms_crypto_key" "credentials" {
  name            = "user-credentials"
  key_ring        = google_kms_key_ring.autoapply.id
  rotation_period = "7776000s" # 90 days
  lifecycle { prevent_destroy = true }
}

resource "google_secret_manager_secret" "anthropic_api_key" {
  secret_id = "anthropic-api-key"
  replication { auto {} }
}

resource "google_secret_manager_secret" "database_url" {
  secret_id = local.db_url_secret
  replication { auto {} }
}

# ---------------------------------------------------------------- compute

resource "google_cloud_run_v2_service" "api" {
  name     = "autoapply-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.api.email
    scaling {
      min_instance_count = 0
      max_instance_count = 4
    }

    containers {
      image = var.image

      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }

      env {
        name  = "AUTOAPPLY_ARTIFACT_URI"
        value = "gs://${google_storage_bucket.artifacts.name}"
      }
      env {
        name = "AUTOAPPLY_DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "ANTHROPIC_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.anthropic_api_key.secret_id
            version = "latest"
          }
        }
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [google_sql_database_instance.postgres.connection_name] }
    }
  }

  depends_on = [google_project_service.enabled]
}

# Ingest and match are short and bursty — Cloud Run Jobs is the right shape.
resource "google_cloud_run_v2_job" "ingest" {
  name     = "autoapply-ingest"
  location = var.region

  template {
    template {
      service_account = google_service_account.worker.email
      max_retries     = 2
      containers {
        image   = var.image
        command = ["autoapply"]
        args    = ["ingest"]
        resources { limits = { cpu = "1", memory = "1Gi" } }
        env {
          name = "AUTOAPPLY_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url.secret_id
              version = "latest"
            }
          }
        }
        env {
          name  = "AUTOAPPLY_ARTIFACT_URI"
          value = "gs://${google_storage_bucket.artifacts.name}"
        }
      }
      volumes {
        name = "cloudsql"
        cloud_sql_instance { instances = [google_sql_database_instance.postgres.connection_name] }
      }
    }
  }
}

# Playwright needs a warm instance with real memory. A single small MIG is the
# cheapest thing that behaves; swap for GKE Autopilot when it needs to scale.
resource "google_compute_instance_template" "submit_worker" {
  name_prefix  = "autoapply-submit-"
  machine_type = var.worker_machine_type

  disk {
    source_image = "projects/cos-cloud/global/images/family/cos-stable"
    auto_delete  = true
    boot         = true
    disk_size_gb = 30
  }

  network_interface {
    network = var.vpc_self_link == null ? "default" : var.vpc_self_link
  }

  service_account {
    email  = google_service_account.worker.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    # Runs `autoapply submit` on a schedule; the command only acts on
    # applications a human already approved.
    gce-container-declaration = yamlencode({
      spec = {
        containers = [{
          image   = var.image
          command = ["autoapply"]
          args    = ["submit", "--limit", "5"]
          env = [
            { name = "AUTOAPPLY_ARTIFACT_URI", value = "gs://${google_storage_bucket.artifacts.name}" },
          ]
        }]
        restartPolicy = "Never"
      }
    })
  }

  lifecycle { create_before_destroy = true }
}

resource "google_compute_instance_group_manager" "submit_worker" {
  name               = "autoapply-submit"
  base_instance_name = "autoapply-submit"
  zone               = var.zone
  target_size        = var.worker_count

  version {
    instance_template = google_compute_instance_template.submit_worker.id
  }
}
