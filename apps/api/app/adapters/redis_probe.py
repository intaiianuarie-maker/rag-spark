"""Redis readiness probe (redis-py async client)."""

from __future__ import annotations

import logging
import time

import redis.asyncio as redis_asyncio
from core.health import DependencyStatus

logger = logging.getLogger("knowledge_os.api.adapters.redis_probe")


async def probe(redis_url: str, timeout_seconds: float) -> DependencyStatus:
    name = "redis"
    start = time.perf_counter()
    client = redis_asyncio.from_url(
        redis_url,
        socket_connect_timeout=timeout_seconds,
        socket_timeout=timeout_seconds,
        decode_responses=True,
    )
    try:
        pong = await client.ping()
        latency = (time.perf_counter() - start) * 1000
        return DependencyStatus(
            name=name,
            reachable=bool(pong),
            latency_ms=round(latency, 2),
            detail=f"PING -> {pong!r}",
        )
    except Exception as exc:
        latency = (time.perf_counter() - start) * 1000
        return DependencyStatus(
            name=name,
            reachable=False,
            latency_ms=round(latency, 2),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        try:
            await client.aclose()
        except Exception as exc:  # best-effort close, logged at debug
            logger.debug(
                "redis_probe_close_failed",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
