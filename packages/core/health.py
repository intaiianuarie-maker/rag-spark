"""Shared health/readiness types used by API adapters and the worker."""

from __future__ import annotations

from pydantic import BaseModel


class DependencyStatus(BaseModel):
    """Reachability of one backing service (postgres/redis/qdrant/minio)."""

    name: str
    reachable: bool
    latency_ms: float | None = None
    detail: str | None = None
    error: str | None = None
