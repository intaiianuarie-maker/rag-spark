"""Unit tests for the auth module (no external services)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.unit


def test_password_hash_is_argon2_and_round_trips() -> None:
    from app.auth.passwords import hash_password, needs_rehash, verify_password

    password = "Correct-Horse-Battery-9"
    digest = hash_password(password)
    assert digest.startswith("$argon2")
    assert password not in digest
    assert verify_password(digest, password) is True
    assert needs_rehash(digest) is False


def test_wrong_password_returns_false() -> None:
    from app.auth.passwords import hash_password, verify_password

    digest = hash_password("right-password")
    assert verify_password(digest, "wrong-password") is False


def test_malformed_hash_returns_false_not_raises() -> None:
    from app.auth.passwords import verify_password

    assert verify_password("not-a-hash", "anything") is False


def test_access_and_refresh_tokens_round_trip() -> None:
    from app.auth.tokens import decode_token, issue_access_token, issue_refresh_token

    access = issue_access_token("user-1")
    access_payload = decode_token(access.token, expected_type="access")
    assert access_payload["sub"] == "user-1"
    assert access_payload["type"] == "access"

    refresh = issue_refresh_token("user-1", "family-1")
    refresh_payload = decode_token(refresh.token, expected_type="refresh")
    assert refresh_payload["sub"] == "user-1"
    assert refresh_payload["fid"] == "family-1"


def test_token_type_mismatch_rejected() -> None:
    from app.auth.tokens import AuthTokenError, decode_token, issue_access_token

    access = issue_access_token("user-1")
    with pytest.raises(AuthTokenError) as excinfo:
        decode_token(access.token, expected_type="refresh")
    assert excinfo.value.code == "invalid_token"


def test_tampered_token_rejected() -> None:
    from app.auth.tokens import AuthTokenError, decode_token, issue_access_token

    token = issue_access_token("user-1").token
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    with pytest.raises(AuthTokenError) as excinfo:
        decode_token(tampered)
    assert excinfo.value.code == "invalid_token"


def test_expired_token_rejected() -> None:
    import jwt
    from app.auth.config import get_auth_settings
    from app.auth.tokens import AuthTokenError, decode_token

    settings = get_auth_settings()
    past = datetime.now(UTC) - timedelta(minutes=1)
    payload = {
        "sub": "user-1",
        "type": "refresh",
        "jti": "expired-jti",
        "iat": int((past - timedelta(minutes=5)).timestamp()),
        "exp": int(past.timestamp()),
        "iss": settings.issuer,
        "aud": settings.audience,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(AuthTokenError) as excinfo:
        decode_token(token, expected_type="refresh")
    assert excinfo.value.code == "token_expired"


def test_normalize_email() -> None:
    from app.auth.service import normalize_email

    assert normalize_email("  User.Name@Example.COM ") == "user.name@example.com"


def test_default_secret_rejected_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.auth.config import DEV_JWT_SECRET, AuthSettings, get_auth_settings
    from core.config import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    get_auth_settings.cache_clear()
    try:
        with pytest.raises(ValueError):
            AuthSettings(  # type: ignore[call-arg]
                jwt_secret=DEV_JWT_SECRET, _env_file=None
            )
    finally:
        get_settings.cache_clear()
        get_auth_settings.cache_clear()
