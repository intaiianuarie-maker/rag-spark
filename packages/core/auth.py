"""Authentication and authorization *stub types* only.

M0 provides typed boundaries; enforcement is implemented in M1 (auth/workspaces).
These types are product-owned (per `docs/reuse/REUSE_STRATEGY.md` the ACL model
is one of the few subsystems we must own) and contain no vendor code.

Cross-workspace leakage (R-004) is prevented by applying :class:`ACLScope`
*before* retrieval, so the retrieval interfaces consume this type directly.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Role(StrEnum):
    """Workspace membership role (canonical: owner > admin > editor > viewer)."""

    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class Principal(BaseModel):
    """Authenticated caller identity (roles populated from memberships in M1)."""

    user_id: str
    email: str | None = None
    is_authenticated: bool = False
    workspace_roles: dict[str, Role] = Field(default_factory=dict)
    #: Set when the caller authenticated with a workspace API key (M1-001c).
    key_id: str | None = None
    #: False for a revoked/expired key or session; `can()` denies everything.
    is_active: bool = True

    def role_in(self, workspace_id: str) -> Role | None:
        return self.workspace_roles.get(workspace_id)

    def can_read(self, workspace_id: str) -> bool:
        role = self.role_in(workspace_id)
        return role is not None and role in {Role.OWNER, Role.ADMIN, Role.EDITOR, Role.VIEWER}

    def can_write(self, workspace_id: str) -> bool:
        role = self.role_in(workspace_id)
        return role is not None and role in {Role.OWNER, Role.ADMIN, Role.EDITOR}


class ACLScope(BaseModel):
    """Authorization scope applied before any retrieval (mitigates R-004).

    Every retrieval call carries an ``ACLScope``. Stores must translate it into
    a hard metadata filter; the orchestrator must never rank across workspaces.
    """

    user_id: str | None = None
    workspace_ids: list[str] = Field(default_factory=list)
    include_public: bool = False

    def allows_workspace(self, workspace_id: str) -> bool:
        return self.include_public or workspace_id in self.workspace_ids

    @property
    def is_empty(self) -> bool:
        return not self.workspace_ids and not self.include_public
