"""Redis ingestion queue adapter (M2-001c).

Vendor Redis access lives here (``/adapters/`` is the CI vendor fence); the
public ``ingestion.queue`` module re-exports this typed surface.

Keys are **namespaced** (``ingest:<env>:queue``) so tests cannot collide with a
live worker on the shared Redis. ``INGEST_QUEUE_PREFIX`` overrides the prefix.
"""

from __future__ import annotations

import asyncio
import os
import time
import weakref

import redis.asyncio as redis_asyncio
from core.config import get_settings
from redis.exceptions import RedisError

#: Atomic promote: move due delayed jobs to the ready queue without a
#: zrem→lpush crash window (KEYS[1]=queue, KEYS[2]=delayed; ARGV[1]=now).
_PROMOTE_DUE_LUA = """
local due = redis.call('ZRANGEBYSCORE', KEYS[2], 0, ARGV[1])
local moved = 0
for _, job in ipairs(due) do
  if redis.call('ZREM', KEYS[2], job) == 1 then
    redis.call('LPUSH', KEYS[1], job)
    moved = moved + 1
  end
end
return moved
"""

_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, redis_asyncio.Redis] = (
    weakref.WeakKeyDictionary()
)


class IngestQueueUnavailable(RuntimeError):
    """Redis (the job queue) cannot be reached."""


def _prefix() -> str:
    override = os.environ.get("INGEST_QUEUE_PREFIX")
    if override:
        return override
    return f"{get_settings().app_env}:"


def queue_key() -> str:
    return f"ingest:{_prefix()}queue"


def delayed_key() -> str:
    return f"ingest:{_prefix()}delayed"


def get_client() -> redis_asyncio.Redis:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        settings = get_settings()
        client = redis_asyncio.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
        )
        _clients[loop] = client
    return client


def _as_str(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


async def close_client() -> None:
    loop = asyncio.get_running_loop()
    client = _clients.pop(loop, None)
    if client is not None:
        await client.aclose()


async def enqueue(job_id: str) -> None:
    try:
        await get_client().lpush(queue_key(), job_id)
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc


async def dequeue(timeout_seconds: int = 1) -> str | None:
    try:
        item = await get_client().brpop(queue_key(), timeout=timeout_seconds)
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc
    if item is None:
        return None
    return _as_str(item[1])


async def schedule_retry(job_id: str, delay_seconds: float) -> None:
    try:
        await get_client().zadd(delayed_key(), {job_id: time.time() + delay_seconds})
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc


async def promote_due() -> int:
    """Atomically move due delayed jobs back onto the ready queue."""
    try:
        moved = await get_client().eval(
            _PROMOTE_DUE_LUA, 2, queue_key(), delayed_key(), time.time()
        )
        return int(moved)
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc


async def queue_depth() -> int:
    try:
        return int(await get_client().llen(queue_key()))
    except RedisError as exc:
        raise IngestQueueUnavailable("ingestion queue unavailable") from exc
