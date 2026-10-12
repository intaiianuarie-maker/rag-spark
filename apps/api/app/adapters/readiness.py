"""Aggregate dependency readiness checks for ``GET /ready``.

Probes postgres/redis/qdrant/minio/infinity concurrently. Failures are
reported, never raised, so readiness stays a 200 with per-service status
(M0 requirement: "non-fatal with status"). The API container is still
considered started even if a dependency is down, letting the UI show an
explicit degraded state.

LLM gateway explicitly NOT probed (review1 F3 BLOCKED): ``LITELLM_URL``
defaults to an external SaaS endpoint (OpenRouter) or to a coordinator-owned
``:4000`` gateway topology (review finding 4, coordinator-owned), and neither
shape offers a cheap contractually-guaranteed unauthenticated health
endpoint. Probing a billable/external completion endpoint on every
readiness poll would add latency, cost, and flapping. Revisit once the
coordinator settles the gateway topology with a dedicated health path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from core.config import get_settings
from core.health import DependencyStatus

from app.adapters import infinity_probe, minio_probe, postgres, qdrant_probe, redis_probe


async def check_dependencies() -> dict[str, DependencyStatus]:
    settings = get_settings()
    timeout = settings.readiness_timeout_seconds

    probes: dict[str, Coroutine[Any, Any, DependencyStatus]] = {
        "postgres": postgres.probe(timeout),
        "redis": redis_probe.probe(settings.redis_url, timeout),
        "qdrant": qdrant_probe.probe(settings.qdrant_url, timeout),
        "minio": minio_probe.probe(settings.minio_endpoint, timeout, settings.minio_secure),
        "infinity": infinity_probe.probe(settings.infinity_url, timeout),
    }

    results = await asyncio.gather(*probes.values(), return_exceptions=True)
    statuses: dict[str, DependencyStatus] = {}
    for name, result in zip(probes.keys(), results, strict=True):
        if isinstance(result, DependencyStatus):
            statuses[name] = result
        else:
            statuses[name] = DependencyStatus(
                name=name,
                reachable=False,
                error=f"{type(result).__name__}: {result}",
            )
    return statuses
