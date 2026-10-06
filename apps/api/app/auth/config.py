"""Auth configuration (environment-driven).

Kept local to the auth module so M0 core config stays untouched; all values are
overridable via ``AUTH_*`` environment variables. The default secret is for
local/test only and is rejected when ``APP_ENV`` is staging/production.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-insecure-change-me"
_INSECURE_ENVS = {"staging", "production"}


class AuthSettings(BaseSettings):
    """Typed auth settings, prefixed with ``AUTH_``."""

    model_config = SettingsConfigDict(
        env_prefix="AUTH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    issuer: str = "knowledge-os"
    audience: str = "knowledge-os-api"

    access_token_ttl_seconds: int = 900  # 15 minutes
    refresh_token_ttl_seconds: int = 1_209_600  # 14 days

    login_rate_limit_max_attempts: int = 5
    login_rate_limit_window_seconds: int = 60

    @model_validator(mode="after")
    def _forbid_default_secret_outside_local(self) -> AuthSettings:
        # Imported lazily to avoid an import cycle at module load.
        from core.config import get_settings

        if get_settings().app_env in _INSECURE_ENVS and self.jwt_secret == DEV_JWT_SECRET:
            raise ValueError(
                "AUTH_JWT_SECRET must be set when APP_ENV is staging or production"
            )
        return self


@lru_cache
def get_auth_settings() -> AuthSettings:
    return AuthSettings()
