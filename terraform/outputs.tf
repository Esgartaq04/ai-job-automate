output "api_url" {
  value       = google_cloud_run_v2_service.api.uri
  description = "Public URL of the review UI and API."
}

output "artifacts_bucket" {
  value       = google_storage_bucket.artifacts.name
  description = "Set AUTOAPPLY_ARTIFACT_URI to gs://<this>."
}

output "sql_connection_name" {
  value       = google_sql_database_instance.postgres.connection_name
  description = "For the Cloud SQL proxy and the AUTOAPPLY_DATABASE_URL secret."
}

output "credentials_kms_key" {
  value       = google_kms_crypto_key.credentials.id
  description = "Envelope-encryption key for the credentials table."
}
