"""Runtime configuration. Everything is env-driven; see .env.example."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTOAPPLY_", env_file=".env", extra="ignore", case_sensitive=False
    )

    env: str = "local"
    database_url: str = "postgresql+psycopg://autoapply:autoapply@localhost:5432/autoapply"

    # Anthropic
    model_fast: str = "claude-haiku-4-5"
    model_smart: str = "claude-opus-5"

    # Embeddings
    embedding_provider: str = "local"
    embedding_dim: int = 1536

    # Artifacts
    artifact_uri: str = "local:///var/lib/autoapply/artifacts"

    # Submission
    dry_run: bool = False
    # Override the Chromium binary (system package, or a preinstalled build whose
    # revision differs from the pinned playwright wheel). Empty = playwright's own.
    chromium_path: str = ""
    daily_submit_cap: int = 25
    field_confidence_floor: float = 0.7
    freetext_escalate_chars: int = 280

    # Pacing
    ats_rate_per_min: int = 20
    circuit_failure_threshold: int = 5
    submit_jitter_seconds: int = 45

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_token: str = "dev-token-change-me"


@lru_cache
def get_settings() -> Settings:
    return Settings()
