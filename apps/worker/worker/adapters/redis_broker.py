"""Redis broker adapter for the worker.

M0 placeholder for a future job queue. ARQ (MIT) or Dramatiq (LGPL-3.0) can be
wired behind this adapter later without changing `worker.main`. No jobs are
registered in M0.
"""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as redis_asyncio

logger = logging.getLogger("knowledge_os.worker.redis")


class RedisBroker:
    """Minimal async Redis connectivity wrapper."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: redis_asyncio.Redis | None = None

    async def connect_with_retry(self, timeout_seconds: float = 30.0) -> bool:
        """Attempt to connect/ping until ``timeout_seconds`` elapses."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        delay = 0.5
        while loop.time() < deadline:
            client: redis_asyncio.Redis | None = None
            try:
                client = redis_asyncio.from_url(
                    self._url,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                    decode_responses=True,
                )
                await client.ping()
                self._client = client
                return True
            except Exception as exc:
                logger.warning(
                    "redis_connect_retry",
                    extra={"error": f"{type(exc).__name__}: {exc}"},
                )
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception as exc:  # best-effort cleanup, logged at debug
                        logger.debug(
                            "redis_broker_close_failed",
                            extra={"error": f"{type(exc).__name__}: {exc}"},
                        )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
        return False

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
