"""Resolve the caller into a :class:`core.auth.Principal` (M1-001c).

Supports two credentials, both presented as a bearer token:

- a session-bound access JWT (``sid`` claim checked against the Redis session,
  and the session's **owner** must equal the token's ``sub`` — T-R01: existence
  is not ownership);
- a workspace API key (``kio_…``), verified by prefix + argon2 hash.

A revoked/expired session or key yields 401. The principal carries the caller's
active workspace roles (or the key's single workspace role), which is exactly
what :func:`app.authz.policy.can` consumes.
"""

from __future__ import annotations

import uuid

from core.auth import Principal, Role
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.adapters.redis_store import AuthStoreUnavailable
from app.auth.db import get_session
from app.auth.keys import get_active_key, looks_like_api_key
from app.auth.models import User
from app.auth.sessions import session_is_active, session_owner
from app.auth.tokens import GENERIC_TOKEN_MESSAGE, AuthTokenError, decode_token


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "invalid_token", "message": GENERIC_TOKEN_MESSAGE},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "auth_unavailable",
            "message": "Authentication temporarily unavailable.",
        },
    )


async def _workspace_roles(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Role]:
    rows = (
        await session.execute(
            text(
                "SELECT workspace_id, role FROM memberships "
                "WHERE user_id = :uid AND status = 'active'"
            ),
            {"uid": str(user_id)},
        )
    ).all()
    roles: dict[str, Role] = {}
    for workspace_id, role in rows:
        try:
            roles[str(workspace_id)] = Role(role)
        except ValueError:  # defensive: skip an unknown DB role rather than 500
            continue
    return roles


async def _principal_from_token(session: AsyncSession, token: str) -> Principal | None:
    try:
        payload = decode_token(token, expected_type="access")
    except AuthTokenError:
        return None

    session_id = payload.get("sid")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not await session_is_active(session_id):
        return None

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        return None

    # T-R01 (P0) — OWNERSHIP BINDING. A live session is not enough: the token's
    # subject must BE the session's owner. Without this, an access token whose
    # ``sub`` names someone else authenticates as long as the session exists,
    # so any leak that pairs a valid ``sid`` with a foreign ``sub`` is a
    # cross-session forgery. Existence is not ownership.
    owner = await session_owner(session_id)
    if owner is None or owner != str(user_id):
        return None

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return None

    return Principal(
        user_id=str(user.id),
        email=user.email,
        is_authenticated=True,
        is_active=True,
        workspace_roles=await _workspace_roles(session, user.id),
    )


async def _principal_from_key(session: AsyncSession, token: str) -> Principal | None:
    key = await get_active_key(session, token)
    if key is None:
        return None

    from app.auth.models import User

    user = await session.get(User, key.created_by)
    if user is None or not user.is_active:
        return None

    return Principal(
        user_id=str(key.created_by),
        is_authenticated=True,
        is_active=True,
        key_id=str(key.id),
        workspace_roles={str(key.workspace_id): Role(key.role)},
    )


async def get_principal(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """FastAPI dependency returning the authenticated principal."""
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() not in {"bearer", "apikey"} or not token:
        raise _unauthorized()

    try:
        principal = (
            await _principal_from_key(session, token)
            if looks_like_api_key(token)
            else await _principal_from_token(session, token)
        )
    except AuthStoreUnavailable as exc:
        raise _unavailable() from exc

    if principal is None:
        raise _unauthorized()
    return principal
