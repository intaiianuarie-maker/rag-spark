"""PD-007: rejected API-key classes must not skip password verification work."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from app.auth import keys
from app.auth.passwords import hash_password, verify_password
from app.authz.principal import get_principal
from fastapi import HTTPException
from starlette.requests import Request


def _key(**changes):
    values = dict(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        name="fixture",
        prefix="0123456789ab",
        key_hash="stored-hash",
        role="viewer",
        revoked_at=None,
        expires_at=None,
    )
    values.update(changes)
    return keys.ApiKey(**values)


def _session(key):
    result = Mock()
    result.scalar_one_or_none.return_value = key
    return Mock(execute=AsyncMock(return_value=result))


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "malformed", "revoked", "expired", "wrong", "valid"])
async def test_every_key_class_queries_and_verifies_once(monkeypatch, case):
    key = None if case in {"missing", "malformed"} else _key()
    if case == "revoked":
        key.revoked_at = datetime.now(UTC)
    if case == "expired":
        key.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    presented = "kio_" if case == "malformed" else "kio_0123456789ab_fixture-secret"
    verifier = Mock(return_value=case in {"valid", "revoked", "expired"})
    monkeypatch.setattr(keys, "verify_password", verifier)
    session = _session(key)
    result = await keys.get_active_key(session, presented)
    session.execute.assert_awaited_once()
    verifier.assert_called_once()
    assert verifier.call_args.args[1] == presented
    if case in {"valid", "wrong"}:
        assert verifier.call_args.args[0] == key.key_hash
    else:
        assert verifier.call_args.args[0].startswith("$argon2id$")
    assert result is (key if case == "valid" else None)


@pytest.mark.asyncio
async def test_real_argon_verification_keeps_valid_and_invalid_controls():
    token = "kio_0123456789ab_fixture-secret"
    key = _key(key_hash=hash_password(token))
    assert verify_password(key.key_hash, token)
    assert await keys.get_active_key(_session(key), token) is key
    assert await keys.get_active_key(_session(key), token + "wrong") is None
    assert await keys.get_active_key(_session(None), token) is None
    key.revoked_at = datetime.now(UTC)
    assert await keys.get_active_key(_session(key), token) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "malformed", "revoked", "expired", "wrong"])
async def test_rejection_keeps_public_401_semantics(monkeypatch, case):
    key = None if case in {"missing", "malformed"} else _key()
    if case == "revoked":
        key.revoked_at = datetime.now(UTC)
    if case == "expired":
        key.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    token = "kio_" if case == "malformed" else "kio_0123456789ab_wrong"
    monkeypatch.setattr(keys, "verify_password", Mock(return_value=False))
    request = Request(
        {"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]}
    )
    with pytest.raises(HTTPException) as rejected:
        await get_principal(request, _session(key))
    assert rejected.value.status_code == 401
    assert rejected.value.detail["error"] == "invalid_token"
    assert rejected.value.headers == {"WWW-Authenticate": "Bearer"}
