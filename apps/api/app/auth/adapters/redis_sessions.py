"""Redis-backed session store adapter (M1-001c).

Session keys are added on top of the existing Redis client in
``redis_store.py`` so ``sessions.py`` never imports the vendor SDK directly
(vendor isolation, CI guard).
"""

from __future__ import annotations

import json

from redis.exceptions import RedisError

from app.auth.adapters.redis_store import AuthStoreUnavailable, get_redis

SESSION_PREFIX = "auth:session:"


async def create_session(session_id: str, user_id: str, ttl_seconds: int) -> None:
    client = get_redis()
    try:
        await client.set(
            SESSION_PREFIX + session_id,
            json.dumps({"user_id": user_id}),
            ex=ttl_seconds,
        )
    except RedisError as exc:
        raise AuthStoreUnavailable("session store unavailable") from exc


async def session_is_active(session_id: str) -> bool:
    client = get_redis()
    try:
        return bool(await client.exists(SESSION_PREFIX + session_id))
    except RedisError as exc:
        raise AuthStoreUnavailable("session store unavailable") from exc


async def revoke_session(session_id: str) -> None:
    client = get_redis()
    try:
        await client.delete(SESSION_PREFIX + session_id)
    except RedisError as exc:
        raise AuthStoreUnavailable("session store unavailable") from exc
