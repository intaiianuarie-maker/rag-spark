"""infinity readiness probe (embeddings + rerank, :7997).

Uses plain httpx against the infinity server's HTTP surface rather than the
embedding/rerank adapters, keeping readiness free of model-call semantics.
Any HTTP response (including 404/405 on the root path — infinity only serves
POST /embeddings and POST /rerank) proves the server is up and reachable;
only transport/timeout failures count as down.
"""

from __future__ import annotations

import time

import httpx
from core.health import DependencyStatus


async def probe(infinity_url: str, timeout_seconds: float) -> DependencyStatus:
    name = "infinity"
    start = time.perf_counter()
    url = infinity_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url)
        latency = (time.perf_counter() - start) * 1000
        return DependencyStatus(
            name=name,
            reachable=True,
            latency_ms=round(latency, 2),
            detail=f"GET / -> {response.status_code}",
        )
    except Exception as exc:
        latency = (time.perf_counter() - start) * 1000
        return DependencyStatus(
            name=name,
            reachable=False,
            latency_ms=round(latency, 2),
            error=f"{type(exc).__name__}: {exc}",
        )
