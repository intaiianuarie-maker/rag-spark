"""Integration tests for the auth lifecycle.

Requires postgres + redis (compose up); skips with an explicit message when the
services are down (never a silent pass). The schema is applied via
``alembic upgrade head`` before the first test.
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.integration

PASSWORD = "Correct-Horse-Battery-9"
GENERIC_CREDENTIALS = "Invalid email or password."


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _unique_email() -> str:
    return f"m1a-{uuid.uuid4().hex}@example.test"


@pytest.fixture(scope="module")
def client():
    if not (_port_open(5432) and _port_open(6379)):
        pytest.skip("postgres/redis not reachable; run `make up`")

    from app.db import run_migrations

    run_migrations()  # idempotent; applies 0002_users

    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


def _register(client, email: str, password: str = PASSWORD):
    return client.post("/auth/register", json={"email": email, "password": password})


def _login(client, email: str, password: str = PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_register_login_refresh_logout_lifecycle(client) -> None:
    email = _unique_email()

    response = _register(client, email)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == email
    assert "password" not in body and "password_hash" not in body

    response = _login(client, email)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert set(tokens) >= {"access_token", "refresh_token", "token_type", "expires_in"}
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] > 0

    # Refresh rotates the token; the old refresh token is rejected.
    response = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 200, response.text
    rotated = response.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]

    response = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "invalid_token"

    # Logout invalidates the family server-side (Redis).
    response = client.post("/auth/logout", json={"refresh_token": rotated["refresh_token"]})
    assert response.status_code == 204
    assert response.content == b""

    response = client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert response.status_code == 401


def test_wrong_password_and_unknown_user_are_indistinguishable(client) -> None:
    email = _unique_email()
    _register(client, email)

    bad = _login(client, email, "definitely-wrong")
    unknown = _login(client, _unique_email(), "definitely-wrong")

    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json()
    assert bad.json()["detail"]["message"] == GENERIC_CREDENTIALS
    # No credential material leaks into the error body.
    assert PASSWORD not in bad.text
    assert "definitely-wrong" not in bad.text
    assert "$argon2" not in bad.text


def test_tampered_refresh_token_rejected(client) -> None:
    email = _unique_email()
    _register(client, email)
    tokens = _login(client, email).json()

    tampered = tokens["refresh_token"][:-2] + "aa"
    response = client.post("/auth/refresh", json={"refresh_token": tampered})
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "invalid_token"


def test_expired_refresh_token_rejected(client) -> None:
    import jwt
    from app.auth.config import get_auth_settings

    settings = get_auth_settings()
    past = datetime.now(UTC) - timedelta(minutes=1)
    payload = {
        "sub": str(uuid.uuid4()),
        "type": "refresh",
        "jti": uuid.uuid4().hex,
        "fid": uuid.uuid4().hex,
        "iat": int((past - timedelta(minutes=5)).timestamp()),
        "exp": int(past.timestamp()),
        "iss": settings.issuer,
        "aud": settings.audience,
    }
    expired = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    response = client.post("/auth/refresh", json={"refresh_token": expired})
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "token_expired"


def test_sixth_rapid_login_is_throttled(client) -> None:
    email = _unique_email()
    _register(client, email)

    statuses = [_login(client, email, "wrong-password").status_code for _ in range(6)]
    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429

    throttled = _login(client, email, "wrong-password")
    assert throttled.status_code == 429
    assert throttled.json()["detail"]["error"] == "rate_limited"
    assert "Retry-After" in throttled.headers


def test_duplicate_registration_is_generic_conflict(client) -> None:
    email = _unique_email()
    assert _register(client, email).status_code == 201
    duplicate = _register(client, email)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["error"] == "email_taken"
    assert PASSWORD not in duplicate.text
    assert "$argon2" not in duplicate.text


def test_validation_error_redacts_password(client) -> None:
    submitted = "Zq7"  # too short; distinctive so it can't appear as a substring elsewhere
    response = client.post("/auth/register", json={"email": "a@b.co", "password": submitted})
    assert response.status_code == 422
    # The submitted password must never be echoed back.
    assert submitted not in response.text
    assert '"input"' not in response.text
    assert "input" not in response.json()["detail"][0]
