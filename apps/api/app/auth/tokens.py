"""JWT access/refresh token issuing and validation (PyJWT, HS256).

Claims: ``sub`` (user id), ``type`` (access|refresh), ``jti``, ``fid`` (refresh
family id), ``iat``, ``exp``, ``iss``, ``aud``. Validation failures raise
:class:`AuthTokenError` with a stable code and a generic message.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from app.auth.config import get_auth_settings

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"

GENERIC_TOKEN_MESSAGE = "Invalid or expired token."


class AuthTokenError(Exception):
    """Raised when a token is missing, malformed, tampered, or expired."""

    def __init__(self, code: str, message: str = GENERIC_TOKEN_MESSAGE) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime
    expires_in: int


def _encode(
    user_id: str,
    token_type: str,
    ttl_seconds: int,
    *,
    jti: str | None = None,
    family_id: str | None = None,
) -> IssuedToken:
    settings = get_auth_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    token_jti = jti or uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": user_id,
        "type": token_type,
        "jti": token_jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": settings.issuer,
        "aud": settings.audience,
    }
    if family_id is not None:
        payload["fid"] = family_id
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return IssuedToken(
        token=token, jti=token_jti, expires_at=expires_at, expires_in=ttl_seconds
    )


def issue_access_token(user_id: str) -> IssuedToken:
    settings = get_auth_settings()
    return _encode(user_id, ACCESS_TOKEN_TYPE, settings.access_token_ttl_seconds)


def issue_refresh_token(
    user_id: str, family_id: str, *, jti: str | None = None
) -> IssuedToken:
    settings = get_auth_settings()
    return _encode(
        user_id,
        REFRESH_TOKEN_TYPE,
        settings.refresh_token_ttl_seconds,
        jti=jti,
        family_id=family_id,
    )


def new_family_id() -> str:
    return uuid.uuid4().hex


def decode_token(token: str, *, expected_type: str | None = None) -> dict[str, Any]:
    """Decode and validate a JWT, raising :class:`AuthTokenError` on any failure."""
    settings = get_auth_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.audience,
            issuer=settings.issuer,
            options={"require": ["exp", "iat", "sub", "jti", "type"]},
        )
    except ExpiredSignatureError as exc:
        raise AuthTokenError("token_expired") from exc
    except InvalidTokenError as exc:
        raise AuthTokenError("invalid_token") from exc

    if expected_type is not None and payload.get("type") != expected_type:
        raise AuthTokenError("invalid_token")
    return payload
