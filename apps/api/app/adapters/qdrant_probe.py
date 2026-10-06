"""Qdrant readiness probe.

Uses Qdrant's public HTTP ``/readyz`` endpoint via httpx rather than the
``qdrant-client`` SDK, keeping the M0 skeleton free of the vendor dependency
(the real VectorStore adapter lands in M3).
"""

from __future__ import annotations

import time

import httpx
from core.health import DependencyStatus


async def probe(qdrant_url: str, timeout_seconds: float) -> DependencyStatus:
    name = "qdrant"
    start = time.perf_counter()
    url = qdrant_url.rstrip("/") + "/readyz"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url)
        latency = (time.perf_counter() - start) * 1000
        reachable = response.status_code == 200
        return DependencyStatus(
            name=name,
            reachable=reachable,
            latency_ms=round(latency, 2),
            detail=f"GET /readyz -> {response.status_code}",
        )
    except Exception as exc:
        latency = (time.perf_counter() - start) * 1000
        return DependencyStatus(
            name=name,
            reachable=False,
            latency_ms=round(latency, 2),
            error=f"{type(exc).__name__}: {exc}",
        )
