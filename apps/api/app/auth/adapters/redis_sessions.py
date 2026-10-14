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


async def session_owner(session_id: str) -> str | None:
    """The user id a session belongs to, or ``None`` when it does not exist.

    T-R01 (P0): existence is not ownership. A caller presenting an access
    token whose ``sub`` names someone else must be rejected, and that can only
    be decided by comparing the token's subject against the session's stored
    owner.
    """
    if not session_id:
        return None
    client = get_redis()
    try:
        raw = await client.get(SESSION_PREFIX + session_id)
    except RedisError as exc:
        raise AuthStoreUnavailable("session store unavailable") from exc
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        # A record we cannot read is not a session we will honour.
        return None
    if not isinstance(payload, dict):
        return None
    owner = payload.get("user_id")
    return owner if isinstance(owner, str) and owner else None


async def iter_session_ids(batch: int = 500) -> list[str]:
    """Every live session id (used by the T-R01 rotation procedure)."""
    client = get_redis()
    found: list[str] = []
    try:
        async for key in client.scan_iter(match=SESSION_PREFIX + "*", count=batch):
            name = key.decode() if isinstance(key, bytes) else str(key)
            found.append(name[len(SESSION_PREFIX) :])
    except RedisError as exc:
        raise AuthStoreUnavailable("session store unavailable") from exc
    return found


async def count_sessions() -> int:
    """How many live sessions exist (rotation planning; T-R01)."""
    client = get_redis()
    total = 0
    try:
        async for _ in client.scan_iter(match=SESSION_PREFIX + "*", count=500):
            total += 1
    except RedisError as exc:
        raise AuthStoreUnavailable("session store unavailable") from exc
    return total


async def delete_sessions(session_ids: list[str]) -> int:
    """Delete the named sessions outright; returns how many keys were removed."""
    if not session_ids:
        return 0
    client = get_redis()
    try:
        return int(await client.delete(*[SESSION_PREFIX + sid for sid in session_ids]))
    except RedisError as exc:
        raise AuthStoreUnavailable("session store unavailable") from exc


async def revoke_session(session_id: str) -> None:
    client = get_redis()
    try:
        await client.delete(SESSION_PREFIX + session_id)
    except RedisError as exc:
        raise AuthStoreUnavailable("session store unavailable") from exc
