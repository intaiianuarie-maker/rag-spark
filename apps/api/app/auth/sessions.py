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
    "iter_session_ids",
    "new_session_id",
    "revoke_all_sessions",
    "revoke_session",
    "session_is_active",
    "session_owner",
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


async def session_owner(session_id: str) -> str | None:
    """The user id that owns ``session_id`` (``None`` if there is no session).

    T-R01 (P0): the auth path must bind a token's subject to the session's
    stored owner. ``session_is_active`` only proves a session *exists* — which
    is exactly the gap that let a victim-subject token ride another session.
    """
    if not session_id:
        return None
    return await _store.session_owner(session_id)


async def iter_session_ids() -> list[str]:
    """Every live session id (rotation procedure; T-R01)."""
    return await _store.iter_session_ids()


async def revoke_all_sessions() -> int:
    """Invalidate every session at once; returns how many were removed."""
    return await _store.delete_sessions(await _store.iter_session_ids())


async def revoke_session(session_id: str) -> None:
    if not session_id:
        return
    await _store.revoke_session(session_id)
