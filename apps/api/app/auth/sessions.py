"""Server-side sessions (M1-001c).

A session is a Redis record keyed by a session id that is embedded as the
``sid`` claim in both access and refresh JWTs. The auth dependency checks the
session on every request, so logout/revocation immediately invalidates access
tokens (closing the M1-001a "stateless access token" gap) and TTL expiry is the
"expired session" case.
"""

from __future__ import annotations

import uuid

from app.auth.adapters import redis_sessions as _store
from app.auth.config import get_auth_settings

__all__ = [
    "SESSION_TTL_SECONDS",
    "create_session",
    "new_session_id",
    "revoke_session",
    "session_is_active",
]


def _ttl_seconds() -> int:
    return get_auth_settings().refresh_token_ttl_seconds


def session_ttl_seconds() -> int:
    """Session lifetime (mirrors the refresh-token TTL)."""
    return _ttl_seconds()


# Back-compat constant; prefer `session_ttl_seconds()` (settings are lazy).
SESSION_TTL_SECONDS = 14 * 24 * 60 * 60


def new_session_id() -> str:
    return uuid.uuid4().hex


async def create_session(session_id: str, user_id: str) -> str:
    await _store.create_session(session_id, user_id, _ttl_seconds())
    return session_id


async def session_is_active(session_id: str) -> bool:
    if not session_id:
        return False
    return await _store.session_is_active(session_id)


async def revoke_session(session_id: str) -> None:
    if not session_id:
        return
    await _store.revoke_session(session_id)
