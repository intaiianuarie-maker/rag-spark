"""infinity readiness probe (embeddings + rerank, :7997).

Uses plain httpx against the infinity server's HTTP surface rather than the
embedding/rerank adapters, keeping readiness free of model-call semantics.

Reachability is ``status_code < 500``: any sub-500 HTTP response (including
307 from the root redirect and 404/405 — infinity only serves POST
/embeddings and POST /rerank) proves the server is up and reachable, while a
5xx means the process answered but the service is broken and must read red
(T-V1-005). Transport/timeout failures stay unreachable.
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
        # T-V1-005: a 5xx means the process answered but the service is
        # broken — a broken embeddings service must read red, not green.
        return DependencyStatus(
            name=name,
            reachable=response.status_code < 500,
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
