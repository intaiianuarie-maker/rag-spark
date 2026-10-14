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
    #: T-V1-071: the ONE collection name the vector path uses. Production default
    #: is ``knowledge`` (no behaviour change when unset); tests point it at a
    #: scratch collection so a suite can never persist points into the live
    #: index. Every writer AND reader must agree on it — the ingest pipeline,
    #: the fence/supersession/purge cleanups and the search path.
    vector_collection: str = "knowledge"

    # --- infinity (bge-m3 embeddings + rerank removed per user order; T-ARCH-04) ---
    infinity_url: str = ""

    # --- Embeddings (google/gemini-embedding-2; 3072-d; T-ARCH-04) ---
    embedding_model: str = "google/gemini-embedding-2"
    embedding_dimension: int = 3072
    embedding_revision: str = "google/gemini-embedding-2"

    # --- MinIO / SeaweedFS S3 (document originals; PD-005) ---
    minio_endpoint: str = "http://localhost:8333"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "knowledge-os"
    minio_secure: bool = False
    minio_region: str = "us-east-1"

    # --- Model gateway (probed by /ready, T-V1-027) ---
    # Base URL of the OpenAI-compatible completion gateway (LiteLLM proxy,
    # OpenRouter, ...). /ready only ever GETs `<url><readiness_model_health_path>`
    # — no API key, no completion, no tokens (review1 F3, unblocked by T-V1-027).
    litellm_url: str = "http://localhost:4000"
    readiness_model_health_path: str = "/health/liveliness"

    # --- Knowledge graph (mirrors packages/graph/factory.py GRAPH_MODE/GRAPH_DB_PATH) ---
    # /ready adds `graph_store` exactly when this is an enabled mode
    # (`rule`/`llm`); any other value except `off` is a config error and reads
    # red rather than silently dropping the dependency.
    graph_mode: str = "off"
    graph_db_path: str = "./.graph/graph.db"

    # --- Ops ---
    # Per-probe transport budget for the store probes (postgres/redis/qdrant/
    # minio/infinity).
    readiness_timeout_seconds: float = 1.5
    # Per-probe transport budget for the capability probes (model gateway,
    # worker consumer, graph store) so a slow external health endpoint cannot
    # stretch the store budget.
    readiness_capability_timeout_seconds: float = 2.0
    # Freshness bound: /ready never reports a result older than this. A probe
    # that misses the budget is cancelled and reported unreachable ("stale"),
    # so a hung dependency can neither stall the response nor yield a stale
    # ready-during-outage answer.
    readiness_freshness_seconds: float = 5.0
    migrations_on_boot: bool = True

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins parsed from a comma-separated string."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


def vector_collection() -> str:
    """The one collection name every vector read/write must use (T-V1-071).

    Resolved per call rather than frozen as a module constant, so a suite that
    exports ``VECTOR_COLLECTION`` before the first call gets its own scratch
    collection. Production never sets it and keeps ``knowledge`` — byte-identical
    behaviour when the variable is absent.
    """
    return get_settings().vector_collection


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
