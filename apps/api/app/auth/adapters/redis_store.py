"""Redis-backed auth state: refresh tokens and login rate limiting.

This is the only module in the auth package that imports the Redis SDK
(``/adapters/`` is the sanctioned location). It exposes a small typed surface
so callers never see vendor errors: Redis failures raise
:class:`AuthStoreUnavailable`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from typing import Any

import redis.asyncio as redis_asyncio
from core.config import get_settings
from redis.exceptions import RedisError

logger = logging.getLogger("knowledge_os.api.auth.redis")

REFRESH_KEY_PREFIX = "auth:refresh:"
FAMILY_KEY_PREFIX = "auth:family:"
RATE_LIMIT_KEY_PREFIX = "auth:rl:"

# One client per event loop: avoids reusing a loop-bound pool across test/app
# event loops while still reusing connections within a process.
_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, redis_asyncio.Redis] = (
    weakref.WeakKeyDictionary()
)


class AuthStoreUnavailable(RuntimeError):
    """Raised when Redis (the auth state store) cannot be reached."""


def _as_str(value: object) -> str:
    """Normalize Redis reply values (str with decode_responses, else bytes)."""
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def get_redis() -> redis_asyncio.Redis:
    """Return the Redis client bound to the current event loop."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        settings = get_settings()
        client = redis_asyncio.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        _clients[loop] = client
    return client


async def close_redis() -> None:
    """Close the current loop's client (best effort)."""
    loop = asyncio.get_running_loop()
    client = _clients.pop(loop, None)
    if client is not None:
        try:
            await client.aclose()
        except Exception as exc:  # best-effort close, logged at debug
            logger.debug(
                "auth_redis_close_failed", extra={"error": f"{type(exc).__name__}: {exc}"}
            )


async def store_refresh_token(
    jti: str, user_id: str, family_id: str, ttl_seconds: int
) -> None:
    """Register a refresh token jti as valid for ``ttl_seconds``."""
    client = get_redis()
    payload = json.dumps({"user_id": user_id, "fid": family_id})
    try:
        pipe = client.pipeline()
        pipe.set(REFRESH_KEY_PREFIX + jti, payload, ex=ttl_seconds)
        pipe.sadd(FAMILY_KEY_PREFIX + family_id, jti)
        pipe.expire(FAMILY_KEY_PREFIX + family_id, ttl_seconds)
        await pipe.execute()
    except RedisError as exc:
        raise AuthStoreUnavailable("refresh store unavailable") from exc


async def get_refresh_token(jti: str) -> dict[str, Any] | None:
    """Return the stored record for a refresh jti, or ``None`` if not valid."""
    client = get_redis()
    try:
        raw = await client.get(REFRESH_KEY_PREFIX + jti)
    except RedisError as exc:
        raise AuthStoreUnavailable("refresh store unavailable") from exc
    if raw is None:
        return None
    try:
        record: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return record


async def rotate_refresh_token(
    old_jti: str, new_jti: str, user_id: str, family_id: str, ttl_seconds: int
) -> None:
    """Invalidate ``old_jti`` and register ``new_jti`` in the same family."""
    client = get_redis()
    payload = json.dumps({"user_id": user_id, "fid": family_id})
    try:
        pipe = client.pipeline()
        pipe.delete(REFRESH_KEY_PREFIX + old_jti)
        pipe.set(REFRESH_KEY_PREFIX + new_jti, payload, ex=ttl_seconds)
        pipe.srem(FAMILY_KEY_PREFIX + family_id, old_jti)
        pipe.sadd(FAMILY_KEY_PREFIX + family_id, new_jti)
        pipe.expire(FAMILY_KEY_PREFIX + family_id, ttl_seconds)
        await pipe.execute()
    except RedisError as exc:
        raise AuthStoreUnavailable("refresh store unavailable") from exc


async def revoke_family(family_id: str) -> None:
    """Invalidate every refresh token jti belonging to a token family."""
    client = get_redis()
    try:
        members = await client.smembers(FAMILY_KEY_PREFIX + family_id)
        pipe = client.pipeline()
        if members:
            pipe.delete(*[REFRESH_KEY_PREFIX + _as_str(member) for member in members])
        pipe.delete(FAMILY_KEY_PREFIX + family_id)
        await pipe.execute()
    except RedisError as exc:
        raise AuthStoreUnavailable("refresh store unavailable") from exc


async def hit_rate_limit(
    key: str, limit: int, window_seconds: int
) -> tuple[bool, int]:
    """Fixed-window counter.

    Returns ``(allowed, retry_after_seconds)``. The sixth attempt within the
    window for the same key is rejected.
    """
    client = get_redis()
    full_key = RATE_LIMIT_KEY_PREFIX + key
    try:
        count = await client.incr(full_key)
        if count == 1:
            await client.expire(full_key, window_seconds)
        ttl = await client.ttl(full_key)
    except RedisError as exc:
        raise AuthStoreUnavailable("rate limit store unavailable") from exc
    allowed = count <= limit
    retry_after = ttl if ttl and ttl > 0 else window_seconds
    return allowed, int(retry_after)
