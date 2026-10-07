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
