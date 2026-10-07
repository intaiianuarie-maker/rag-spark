"""Request/response schemas for the workspaces API (M1-001b).

Consumed by the M1-002b frontend pages; error bodies follow the global nested
shape ``{"detail": {"error", "message"}}`` and 422 bodies are the lean redacted
shape (no ``input``/``ctx``).
"""

from __future__ import annotations

from datetime import datetime

from core.auth import Role
from pydantic import BaseModel, Field, field_validator

from app.auth.service import normalize_email
from app.auth.workspaces.models import MembershipStatus

NAME_MIN = 1
NAME_MAX = 200
EMAIL_MAX = 320


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=NAME_MIN, max_length=NAME_MAX)
    organization_name: str | None = Field(default=None, max_length=NAME_MAX)


class WorkspaceRenameRequest(BaseModel):
    name: str = Field(min_length=NAME_MIN, max_length=NAME_MAX)


class MemberInviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=EMAIL_MAX)
    role: Role = Role.VIEWER

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        normalized = normalize_email(value)
        if "@" not in normalized or normalized.startswith("@"):
            raise ValueError("invalid email address")
        return normalized


class MemberRoleUpdateRequest(BaseModel):
    role: Role


class WorkspacePublic(BaseModel):
    id: str
    name: str
    organization_id: str
    role: Role
    created_at: datetime


class MemberPublic(BaseModel):
    user_id: str
    email: str
    role: Role
    status: MembershipStatus
    created_at: datetime


class MemberRosterItem(BaseModel):
    """Frozen M1-001f roster item (exactly the four contract fields)."""

    user_id: str
    email: str
    role: Role
    status: MembershipStatus


class InvitationItem(BaseModel):
    """Frozen M1-001f pending-invitation item."""

    workspace_id: str
    workspace_name: str
    role: Role
    invited_at: datetime


class WorkspaceErrorBody(BaseModel):
    error: str
    message: str
