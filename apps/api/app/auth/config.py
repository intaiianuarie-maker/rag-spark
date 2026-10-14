"""Auth configuration (environment-driven).

Kept local to the auth module so M0 core config stays untouched; all values are
overridable via ``AUTH_*`` environment variables.

T-R01 (P0): the signing secret is validated in **every** environment, not only
staging/production. The previous rule was doubly wrong:

* it was env-gated, so any non-staging env accepted a shipped default; and
* it only knew about the code default, while ``docker-compose.yml`` shipped a
  *different* literal (``dev-only-insecure-jwt-secret-change-me-…``) that the
  validator never saw — the literal gap the PRIO1 review proved the prod path
  walked through.

So the class is closed, not just the literals:

* there is no code default at all (an unset secret is an empty string, and an
  empty secret refuses to boot);
* every literal we have ever shipped is in :data:`SHIPPED_DEFAULT_SECRETS` and
  is refused, case-insensitively, whatever ``APP_ENV`` says;
* anything shorter than :data:`MIN_JWT_SECRET_LENGTH` is refused (HS256 keys
  are 32+ bytes);
* whole-value placeholders ("changeme", "secret", "password", …) are refused.

Local dev and tests therefore must supply a scratch secret explicitly — that is
the intended cost of closing the gap. Generate one with
``python -c "import secrets; print(secrets.token_urlsafe(48))"``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The historical code default. Shipped in git history; still refused.
DEV_JWT_SECRET = "dev-insecure-change-me"
#: The literal shipped in ``docker-compose.yml`` until T-R01. Refusing it is the
#: literal gap; keeping it in the denylist stops it coming back.
COMPOSE_DEFAULT_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me-0123456789"

#: Every signing-secret literal that has ever shipped with this repository.
SHIPPED_DEFAULT_SECRETS: frozenset[str] = frozenset(
    {DEV_JWT_SECRET, COMPOSE_DEFAULT_JWT_SECRET}
)

#: Whole-value placeholders: refused in every environment, case-insensitively.
PLACEHOLDER_SECRETS: frozenset[str] = frozenset(
    {
        "secret",
        "changeme",
        "change-me",
        "change_me",
        "please-change-me",
        "replace-me",
        "set-me",
        "insecure",
        "dev-insecure",
        "development",
        "password",
        "supersecret",
        "todo",
        "replace-this",
        "your-secret-here",
        "example",
    }
)

#: HS256 wants at least 256 bits of key. Shorter is refused in every env.
MIN_JWT_SECRET_LENGTH = 32

#: Retained for back-compat with callers/tests that referenced the old gate.
#: It no longer gates anything: the rules above are unconditional.
INSECURE_ENVS = {"staging", "production"}
_INSECURE_ENVS = INSECURE_ENVS


class InsecureSigningSecret(ValueError):
    """Raised when a shipped/weak signing secret would be accepted."""


def signing_secret_problem(secret: str) -> str:
    """Why ``secret`` must not be used to sign tokens (``""`` when it is fine).

    Env-independent on purpose: the same answer in local, test, staging and
    production, which is what closes the review's "prod accepted the compose
    default" gap.
    """
    candidate = secret.strip()
    if not candidate:
        return (
            "AUTH_JWT_SECRET is not set. There is no shipped default any more; "
            "generate one with "
            '`python -c "import secrets; print(secrets.token_urlsafe(48))"` and '
            "put it in the environment (or .env)."
        )
    lowered = candidate.lower()
    for shipped in SHIPPED_DEFAULT_SECRETS:
        if lowered == shipped.lower():
            return (
                f"AUTH_JWT_SECRET is the shipped default literal {shipped!r}. "
                "Refused in every environment. Generate a fresh secret."
            )
    if lowered in PLACEHOLDER_SECRETS:
        return (
            f"AUTH_JWT_SECRET is the placeholder value {candidate!r}. "
            "Refused in every environment."
        )
    if len(candidate) < MIN_JWT_SECRET_LENGTH:
        return (
            f"AUTH_JWT_SECRET must be at least {MIN_JWT_SECRET_LENGTH} characters "
            f"(got {len(candidate)}). Refused in every environment."
        )
    return ""


class AuthSettings(BaseSettings):
    """Typed auth settings, prefixed with ``AUTH_``."""

    model_config = SettingsConfigDict(
        env_prefix="AUTH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # No default: unset means empty, and empty refuses to boot. That is the
    # only fail-closed shape — a real default would just be another literal.
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    issuer: str = "knowledge-os"
    audience: str = "knowledge-os-api"

    access_token_ttl_seconds: int = 900  # 15 minutes
    refresh_token_ttl_seconds: int = 1_209_600  # 14 days

    login_rate_limit_max_attempts: int = 5
    login_rate_limit_window_seconds: int = 60

    @model_validator(mode="after")
    def _reject_insecure_signing_secret(self) -> AuthSettings:
        problem = signing_secret_problem(self.jwt_secret)
        if problem:
            raise InsecureSigningSecret(problem)
        return self


@lru_cache
def get_auth_settings() -> AuthSettings:
    return AuthSettings()