"""Environment-driven application settings.

Reused: pydantic-settings (MIT) for typed, validated env parsing.
Custom: only the field set and defaults, kept in sync with `docker-compose.yml`
and `.env.example`. No secrets are read at import time beyond env vars.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "ci", "staging", "production"]


class Settings(BaseSettings):
    """Typed application settings, overridable via environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_name: str = "knowledge-os-api"
    app_env: Environment = "local"
    version: str = "0.1.0"
    git_sha: str = "unknown"

    # --- Logging ---
    log_level: str = "INFO"
    log_json: bool = True

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000"

    # --- PostgreSQL (metadata + lexical FTS; PD-002) ---
    # `postgresql+psycopg` works for both the async app engine and sync Alembic.
    database_url: str = "postgresql+psycopg://knowledge:knowledge@localhost:5432/knowledge"

    # --- Redis (worker broker/cache) ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Qdrant (dense vectors; PD-002) ---
    qdrant_url: str = "http://localhost:6333"

    # --- MinIO / SeaweedFS S3 (document originals; PD-005) ---
    minio_endpoint: str = "http://localhost:8333"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "knowledge-os"
    minio_secure: bool = False
    minio_region: str = "us-east-1"

    # --- Ops ---
    readiness_timeout_seconds: float = 1.5
    migrations_on_boot: bool = True

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins parsed from a comma-separated string."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
