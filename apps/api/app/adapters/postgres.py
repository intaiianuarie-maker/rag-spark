"""PostgreSQL readiness probe (via SQLAlchemy async engine)."""

from __future__ import annotations

import asyncio
import time

from core.health import DependencyStatus
from sqlalchemy import text

from app.db import get_engine


async def _select_one() -> None:
    engine = get_engine()
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def probe(timeout_seconds: float) -> DependencyStatus:
    """Run ``SELECT 1`` against the configured database.

    The shared engine in ``app.db`` already uses the configured database URL.
    ``asyncio.wait_for`` applies the readiness timeout to connect+execute so a
    hanging database cannot exceed the caller's deadline.
    """
    name = "postgres"
    start = time.perf_counter()
    try:
        await asyncio.wait_for(_select_one(), timeout=timeout_seconds)
        latency = (time.perf_counter() - start) * 1000
        return DependencyStatus(
            name=name,
            reachable=True,
            latency_ms=round(latency, 2),
            detail="SELECT 1 ok",
        )
    except Exception as exc:
        latency = (time.perf_counter() - start) * 1000
        return DependencyStatus(
            name=name,
            reachable=False,
            latency_ms=round(latency, 2),
            error=f"{type(exc).__name__}: {exc}",
        )
