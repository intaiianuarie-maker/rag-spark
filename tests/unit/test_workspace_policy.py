"""Unit tests for the central authorization service (M1-001c `authz.can`)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from app.authz import (
    Action,
    Resource,
    can,
    ensure_last_owner_safe,
    ensure_owner_can_manage_owner,
    require,
)
from app.authz.errors import ApiError, AuthorizationError
from core.auth import Principal, Role

pytestmark = pytest.mark.unit

WS = "ws-1"
OTHER = "ws-2"

#: Expected decision for a *non-owner* is the role/action matrix; owner is an
#: override (all True). Kept explicit so a matrix change is a visible diff.
MATRIX: dict[Role, dict[Action, bool]] = {
    Role.OWNER: {
        Action.WORKSPACE_READ: True,
        Action.WORKSPACE_MANAGE: True,
        Action.DOCUMENT_READ: True,
        Action.DOCUMENT_WRITE: True,
        Action.KEY_MANAGE: True,
    },
    Role.ADMIN: {
        Action.WORKSPACE_READ: True,
        Action.WORKSPACE_MANAGE: True,
        Action.DOCUMENT_READ: True,
        Action.DOCUMENT_WRITE: True,
        Action.KEY_MANAGE: True,
    },
    Role.EDITOR: {
        Action.WORKSPACE_READ: True,
        Action.WORKSPACE_MANAGE: False,
        Action.DOCUMENT_READ: True,
        Action.DOCUMENT_WRITE: True,
        Action.KEY_MANAGE: False,
    },
    Role.VIEWER: {
        Action.WORKSPACE_READ: True,
        Action.WORKSPACE_MANAGE: False,
        Action.DOCUMENT_READ: True,
        Action.DOCUMENT_WRITE: False,
        Action.KEY_MANAGE: False,
    },
}


def _principal(
    role: Role | None = None,
    *,
    workspace: str = WS,
    active: bool = True,
    authenticated: bool = True,
    key_id: str | None = None,
) -> Principal:
    roles = {} if role is None else {workspace: role}
    return Principal(
        user_id="u1",
        is_authenticated=authenticated,
        is_active=active,
        workspace_roles=roles,
        key_id=key_id,
    )


def _resource(workspace: str = WS) -> Resource:
    return Resource(workspace_id=workspace)


@pytest.mark.parametrize("role", list(MATRIX))
@pytest.mark.parametrize("action", list(Action))
def test_can_matrix_covers_four_roles_and_actions(role: Role, action: Action) -> None:
    assert can(_principal(role), action, _resource()) is MATRIX[role][action]


def test_owner_override_grants_every_action() -> None:
    owner = _principal(Role.OWNER)
    assert all(can(owner, action, _resource()) for action in Action)


def test_cross_workspace_is_denied() -> None:
    # Role for WS must not grant anything on OTHER (key-scope A/B at unit level).
    assert can(_principal(Role.OWNER), Action.DOCUMENT_READ, _resource(OTHER)) is False
    assert can(_principal(Role.ADMIN), Action.WORKSPACE_MANAGE, _resource(OTHER)) is False


def test_key_principal_is_scoped_to_its_workspace() -> None:
    key = _principal(Role.VIEWER, workspace=WS, key_id="key-1")
    assert can(key, Action.DOCUMENT_READ, _resource(WS)) is True
    assert can(key, Action.DOCUMENT_READ, _resource(OTHER)) is False
    assert can(key, Action.WORKSPACE_MANAGE, _resource(WS)) is False


def test_revoked_or_expired_principal_is_denied_everything() -> None:
    revoked = _principal(Role.OWNER, active=False)
    assert all(can(revoked, action, _resource()) is False for action in Action)


def test_unauthenticated_principal_is_denied() -> None:
    anon = _principal(None, authenticated=False)
    assert can(anon, Action.WORKSPACE_READ, _resource()) is False


def test_require_raises_typed_errors() -> None:
    with pytest.raises(AuthorizationError) as member_exc:
        require(_principal(None), Action.WORKSPACE_READ, _resource())
    assert member_exc.value.status_code == 403
    assert member_exc.value.code == "not_a_member"

    with pytest.raises(AuthorizationError) as role_exc:
        require(_principal(Role.VIEWER), Action.WORKSPACE_MANAGE, _resource())
    assert role_exc.value.code == "forbidden"

    # Allowed path does not raise.
    require(_principal(Role.VIEWER), Action.DOCUMENT_READ, _resource())


def test_only_an_owner_may_manage_another_owner() -> None:
    ensure_owner_can_manage_owner(Role.OWNER, Role.OWNER)
    ensure_owner_can_manage_owner(Role.ADMIN, Role.EDITOR)

    with pytest.raises(AuthorizationError):
        ensure_owner_can_manage_owner(Role.ADMIN, Role.OWNER)
    with pytest.raises(AuthorizationError):
        ensure_owner_can_manage_owner(Role.EDITOR, Role.OWNER)


def test_last_owner_guard_blocks_sole_owner_demote_and_remove() -> None:
    with pytest.raises(ApiError) as demote:
        ensure_last_owner_safe(
            target_role=Role.OWNER, new_role=Role.ADMIN, owner_count=1
        )
    assert demote.value.status_code == 409
    assert demote.value.code == "last_owner"

    with pytest.raises(ApiError):
        ensure_last_owner_safe(target_role=Role.OWNER, new_role=None, owner_count=1)


def test_last_owner_guard_allows_safe_changes() -> None:
    ensure_last_owner_safe(target_role=Role.OWNER, new_role=Role.OWNER, owner_count=1)
    ensure_last_owner_safe(target_role=Role.OWNER, new_role=Role.ADMIN, owner_count=2)
    ensure_last_owner_safe(target_role=Role.OWNER, new_role=None, owner_count=2)
    ensure_last_owner_safe(target_role=Role.EDITOR, new_role=None, owner_count=1)


def test_build_purge_filter_scopes_one_document_in_one_workspace() -> None:
    """Q3: purge filters carry exactly document_ids=[doc] + workspace_ids=[ws]."""
    import uuid

    from ingestion.dedup import build_purge_filter

    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    filt = build_purge_filter(workspace_id, document_id)
    assert filt.workspace_ids == [str(workspace_id)]
    assert filt.document_ids == [str(document_id)]


async def test_purge_document_fans_out_to_registered_stores_with_scoped_filter() -> None:
    """Q3: purge_document triggers every registered purger with (ws, doc) ids."""
    import uuid

    from ingestion import dedup
    from ingestion.dedup import build_purge_filter

    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()
    seen: dict[str, object] = {}

    async def fake_fts(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        seen["fts"] = build_purge_filter(ws_id, doc_id)

    async def fake_qdrant(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        seen["qdrant"] = build_purge_filter(ws_id, doc_id)

    class _Scalars:
        def all(self) -> list[object]:
            return []

    class _Result:
        def scalars(self) -> _Scalars:
            return _Scalars()

    class _FakeSession:
        def __init__(self) -> None:
            self.document = type("Doc", (), {"workspace_id": workspace_id})()

        async def get(self, *args: object, **kwargs: object) -> object:
            return self.document

        async def execute(self, *args: object, **kwargs: object) -> _Result:
            return _Result()

        async def commit(self) -> None:
            return None

    session = _FakeSession()

    saved = dict(dedup._STORE_PURGERS)
    dedup._STORE_PURGERS.clear()
    dedup.register_store_purger("fts", fake_fts)
    dedup.register_store_purger("qdrant", fake_qdrant)
    try:
        report = await dedup.purge_document(
            session, workspace_id=workspace_id, document_id=document_id  # type: ignore[arg-type]
        )
    finally:
        dedup._STORE_PURGERS.clear()
        dedup._STORE_PURGERS.update(saved)

    assert report == {"postgres": True, "fts": True, "qdrant": True}
    assert session.document.deleted_at is not None  # type: ignore[attr-defined]
    for name in ("fts", "qdrant"):
        filt = seen[name]
        assert filt.workspace_ids == [str(workspace_id)]  # type: ignore[union-attr]
        assert filt.document_ids == [str(document_id)]  # type: ignore[union-attr]


async def test_purge_document_reports_failed_store_without_false_success() -> None:
    """Q3: a failing store purger reports False (never swallowed as success)."""
    import uuid

    from ingestion import dedup

    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()

    async def ok_purger(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        return None

    async def bad_purger(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        raise RuntimeError("qdrant down")

    class _Scalars:
        def all(self) -> list[object]:
            return []

    class _Result:
        def scalars(self) -> _Scalars:
            return _Scalars()

    class _FakeSession:
        def __init__(self) -> None:
            self.document = type("Doc", (), {"workspace_id": workspace_id})()

        async def get(self, *args: object, **kwargs: object) -> object:
            return self.document

        async def execute(self, *args: object, **kwargs: object) -> _Result:
            return _Result()

        async def commit(self) -> None:
            return None

    session = _FakeSession()

    saved = dict(dedup._STORE_PURGERS)
    dedup._STORE_PURGERS.clear()
    dedup.register_store_purger("fts", ok_purger)
    dedup.register_store_purger("qdrant", bad_purger)
    try:
        report = await dedup.purge_document(
            session, workspace_id=workspace_id, document_id=document_id  # type: ignore[arg-type]
        )
    finally:
        dedup._STORE_PURGERS.clear()
        dedup._STORE_PURGERS.update(saved)

    assert report == {"postgres": True, "fts": True, "qdrant": False}
