"""System endpoints: /health, /ready, /version."""

from __future__ import annotations

from core.config import get_settings
from fastapi import APIRouter

from app.adapters.readiness import check_dependencies
from app.version import get_version_info

router = APIRouter(tags=["system"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Cheap liveness check; does not touch backing services."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.version,
        "env": settings.app_env,
    }


@router.get("/version", summary="Build/version info")
async def version() -> dict[str, str]:
    return get_version_info()


@router.get("/ready", summary="Readiness probe (non-fatal dependency status)")
async def ready() -> dict[str, object]:
    """Report per-dependency reachability.

    Returns HTTP 200 even when a dependency is down (status "degraded"); the
    body carries the detailed statuses so the UI can render an explicit error
    state. This matches the M0 requirement that readiness be non-fatal.
    """
    settings = get_settings()
    dependencies = await check_dependencies()
    all_reachable = all(dep.reachable for dep in dependencies.values())
    return {
        "status": "ok" if all_reachable else "degraded",
        "ready": all_reachable,
        "service": settings.app_name,
        "version": settings.version,
        "dependencies": {name: dep.model_dump() for name, dep in dependencies.items()},
    }
