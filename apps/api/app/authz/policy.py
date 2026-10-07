"""The single authorization decision service (M1-001c).

``can(user, action, resource)`` is the only place role semantics live. Owner is
an override (may do anything in their workspace); admin/editor/viewer follow the
action matrix. A revoked/expired principal (``is_active=False``) is denied
everything, and an API-key principal only has a role for its own workspace, so a
workspace-A key can never act on workspace B.
"""

from __future__ import annotations

from core.auth import Principal, Role
from pydantic import BaseModel

from app.authz.actions import Action
from app.authz.errors import ApiError, AuthorizationError


class Resource(BaseModel):
    """The object an action applies to (M1: a workspace)."""

    workspace_id: str
    kind: str = "workspace"


#: Non-owner action matrix. Owner is handled as an override in `can()`.
ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.ADMIN: frozenset(
        {
            Action.WORKSPACE_READ,
            Action.WORKSPACE_MANAGE,
            Action.DOCUMENT_READ,
            Action.DOCUMENT_WRITE,
            Action.KEY_MANAGE,
        }
    ),
    Role.EDITOR: frozenset(
        {Action.WORKSPACE_READ, Action.DOCUMENT_READ, Action.DOCUMENT_WRITE}
    ),
    Role.VIEWER: frozenset({Action.WORKSPACE_READ, Action.DOCUMENT_READ}),
}


def can(user: Principal, action: Action, resource: Resource) -> bool:
    """Return whether ``user`` may perform ``action`` on ``resource``."""
    if not user.is_authenticated or not user.is_active:
        return False
    role = user.workspace_roles.get(resource.workspace_id)
    if role is None:
        return False
    if role == Role.OWNER:
        return True  # owner override
    return action in ROLE_ACTIONS.get(role, frozenset())


def require(user: Principal, action: Action, resource: Resource) -> None:
    """Raise :class:`AuthorizationError` (403) unless :func:`can` is true."""
    if can(user, action, resource):
        return
    if user.workspace_roles.get(resource.workspace_id) is None:
        raise AuthorizationError(
            "You do not have access to this workspace.", code="not_a_member"
        )
    raise AuthorizationError()


def ensure_owner_can_manage_owner(caller_role: Role, target_role: Role) -> None:
    """Only an owner may change or remove another owner (no admin escalation)."""
    if target_role == Role.OWNER and caller_role != Role.OWNER:
        raise AuthorizationError("Only an owner can manage another owner.")


def ensure_last_owner_safe(
    *, target_role: Role, new_role: Role | None, owner_count: int
) -> None:
    """Refuse to demote/remove the last owner (``new_role=None`` means removal)."""
    if target_role != Role.OWNER or new_role == Role.OWNER:
        return
    if owner_count <= 1:
        raise ApiError(409, "last_owner", "A workspace must keep at least one owner.")
