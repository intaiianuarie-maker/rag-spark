"""T-K04 proof tests."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.authz.principal import _principal_from_key


@pytest.mark.asyncio
async def test_deactivated_user_key_returns_none(monkeypatch) -> None:
    key = SimpleNamespace(
        id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="editor",
    )
    monkeypatch.setattr("app.authz.principal.get_active_key", AsyncMock(return_value=key))

    class Session:
        async def get(self, model, pk):
            return SimpleNamespace(is_active=False)

    result = await _principal_from_key(Session(), "token")  # type: ignore[arg-type]
    assert result is None


@pytest.mark.asyncio
async def test_active_user_key_returns_principal(monkeypatch) -> None:
    key = SimpleNamespace(
        id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="editor",
    )
    monkeypatch.setattr("app.authz.principal.get_active_key", AsyncMock(return_value=key))

    class Session:
        async def get(self, model, pk):
            return SimpleNamespace(is_active=True, id=key.created_by, email="x@example.com")

    principal = await _principal_from_key(Session(), "token")  # type: ignore[arg-type]
    assert principal is not None
    assert principal.is_active is True
    assert str(principal.key_id) == str(key.id)
