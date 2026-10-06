"""Version metadata for the API service."""

from __future__ import annotations

from core import __version__
from core.config import get_settings


def get_version_info() -> dict[str, str]:
    """Return build/version information for ``GET /version``."""
    settings = get_settings()
    return {
        "service": settings.app_name,
        "version": settings.version or __version__,
        "git_sha": settings.git_sha,
        "env": settings.app_env,
    }
