"""System endpoints: /health, /ready, /version."""

from __future__ import annotations

from core.config import get_settings
from fastapi import APIRouter

from app.adapters.readiness import check_dependencies
from app.version import get_version_info
from app.workflows.store import (
    approvals_store_mode,
    checkpoint_db_mode,
    draft_store_mode,
    runs_store_mode,
)

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
async def version() -> dict[str, object]:
    info: dict[str, object] = dict(get_version_info())
    info["store_modes"] = {
        "drafts": draft_store_mode(),
        "runs": runs_store_mode(),
        "approvals": approvals_store_mode(),
        "checkpoints": checkpoint_db_mode(),
    }
    info["draft_migration"] = "applied" if draft_store_mode() == "persistent" else "pending"
    return info


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
        "store_modes": {
            "drafts": draft_store_mode(),
            "runs": runs_store_mode(),
            "approvals": approvals_store_mode(),
            "checkpoints": checkpoint_db_mode(),
        },
        "draft_migration": "applied" if draft_store_mode() == "persistent" else "pending",
    }
