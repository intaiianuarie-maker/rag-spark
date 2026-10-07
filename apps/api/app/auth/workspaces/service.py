"""Workspace/membership service (M1-001b, authorized via M1-001c `authz`).

Authorization decisions are delegated to :func:`app.authz.can`/:func:`require`
(the single authorization path); this service only loads rows, applies the
last-owner invariant, and mutates. Route handlers contain no role checks.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from core.auth import Principal, Role
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.auth.service import normalize_email
from app.auth.workspaces.models import (
    Membership,
    MembershipStatus,
    Organization,
    Workspace,
)
from app.authz import (
    Action,
    ApiError,
    Resource,
    ensure_last_owner_safe,
    ensure_owner_can_manage_owner,
    require,
)


def _resource(workspace_id: uuid.UUID) -> Resource:
    return Resource(workspace_id=str(workspace_id))


class WorkspaceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_workspace(
        self, principal: Principal, name: str, organization_name: str | None = None
    ) -> tuple[Workspace, Role]:
        if principal.key_id is not None:
            raise ApiError(403, "forbidden", "API keys cannot create workspaces.")
        user_id = _user_uuid(principal)
        organization = Organization(name=organization_name or name)
        self._session.add(organization)
        await self._session.flush()

        workspace = Workspace(organization_id=organization.id, name=name)
        self._session.add(workspace)
        await self._session.flush()

        membership = Membership(
            workspace_id=workspace.id,
            user_id=user_id,
            role=Role.OWNER.value,
            status=MembershipStatus.ACTIVE.value,
        )
        self._session.add(membership)
        await self._session.commit()
        await self._session.refresh(workspace)
        return workspace, Role.OWNER

    async def list_workspaces(self, principal: Principal) -> list[tuple[Workspace, Role]]:
        # Scope to the principal's own roles: a session principal has every
        # active membership; an API-key principal only its single workspace, so a
        # workspace-A key can never list workspace B.
        workspace_ids = [uuid.UUID(workspace_id) for workspace_id in principal.workspace_roles]
        if not workspace_ids:
            return []
        statement = (
            select(Workspace)
            .where(Workspace.id.in_(workspace_ids))
            .order_by(Workspace.created_at, Workspace.id)
        )
        workspaces = list((await self._session.execute(statement)).scalars().all())
        return [
            (workspace, principal.workspace_roles[str(workspace.id)])
            for workspace in workspaces
        ]

    async def list_members(
        self, principal: Principal, workspace_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, str, Role, MembershipStatus]]:
        """M1-001f roster: every membership incl. invited, with the user email."""
        workspace = await self._get_workspace(workspace_id)
        require(principal, Action.WORKSPACE_READ, _resource(workspace.id))
        statement = (
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.workspace_id == workspace.id)
            .order_by(Membership.created_at, Membership.user_id)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            (
                membership.user_id,
                user.email,
                Role(membership.role),
                MembershipStatus(membership.status),
            )
            for membership, user in rows
        ]

    async def list_invitations(
        self, principal: Principal
    ) -> list[tuple[uuid.UUID, str, Role, datetime]]:
        """M1-001f: the caller's own pending invitations only."""
        user_id = _user_uuid(principal)
        statement = (
            select(Membership, Workspace)
            .join(Workspace, Workspace.id == Membership.workspace_id)
            .where(
                Membership.user_id == user_id,
                Membership.status == MembershipStatus.INVITED.value,
            )
            .order_by(Membership.created_at, Membership.workspace_id)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            (membership.workspace_id, workspace.name, Role(membership.role), membership.created_at)
            for membership, workspace in rows
        ]

    async def get_workspace(
        self, principal: Principal, workspace_id: uuid.UUID
    ) -> tuple[Workspace, Role]:
        workspace = await self._get_workspace(workspace_id)
        require(principal, Action.WORKSPACE_READ, _resource(workspace.id))
        return workspace, principal.workspace_roles[str(workspace.id)]

    async def rename_workspace(
        self, principal: Principal, workspace_id: uuid.UUID, name: str
    ) -> tuple[Workspace, Role]:
        workspace = await self._get_workspace(workspace_id)
        require(principal, Action.WORKSPACE_MANAGE, _resource(workspace.id))
        workspace.name = name
        await self._session.commit()
        await self._session.refresh(workspace)
        return workspace, principal.workspace_roles[str(workspace.id)]

    async def invite_member(
        self, principal: Principal, workspace_id: uuid.UUID, email: str, role: Role
    ) -> tuple[Membership, User]:
        workspace = await self._get_workspace(workspace_id)
        require(principal, Action.WORKSPACE_MANAGE, _resource(workspace.id))
        ensure_owner_can_manage_owner(
            principal.workspace_roles[str(workspace.id)], role
        )

        target = await self._get_user_by_email(normalize_email(email))
        if target is None:
            raise ApiError(
                404, "user_not_found", "No account exists for that email address."
            )
        if await self._get_membership(workspace.id, target.id) is not None:
            raise ApiError(
                409, "already_member", "That user is already a member of this workspace."
            )

        membership = Membership(
            workspace_id=workspace.id,
            user_id=target.id,
            role=role.value,
            status=MembershipStatus.INVITED.value,
        )
        self._session.add(membership)
        await self._session.commit()
        await self._session.refresh(membership)
        return membership, target

    async def accept_invite(
        self, principal: Principal, workspace_id: uuid.UUID
    ) -> tuple[Membership, User]:
        workspace = await self._get_workspace(workspace_id)
        user_id = _user_uuid(principal)
        membership = await self._get_membership(workspace.id, user_id)
        if membership is None:
            raise ApiError(
                403, "not_invited", "You are not a member of this workspace."
            )
        if membership.status != MembershipStatus.ACTIVE.value:
            membership.status = MembershipStatus.ACTIVE.value
            await self._session.commit()
            await self._session.refresh(membership)
        user = await self._session.get(User, user_id)
        if user is None:
            raise ApiError(401, "invalid_token", "Account no longer exists.")
        return membership, user

    async def change_role(
        self,
        principal: Principal,
        workspace_id: uuid.UUID,
        target_user_id: uuid.UUID,
        role: Role,
    ) -> tuple[Membership, User]:
        workspace = await self._get_workspace(workspace_id)
        await self._lock_workspace(workspace.id)
        require(principal, Action.WORKSPACE_MANAGE, _resource(workspace.id))
        target = await self._require_membership(workspace.id, target_user_id)
        ensure_owner_can_manage_owner(
            principal.workspace_roles[str(workspace.id)], Role(target.role)
        )

        owner_count = await self._count_owners(workspace.id)
        ensure_last_owner_safe(
            target_role=Role(target.role), new_role=role, owner_count=owner_count
        )
        target.role = role.value
        await self._session.commit()
        await self._session.refresh(target)

        target_user = await self._session.get(User, target_user_id)
        if target_user is None:
            raise ApiError(404, "member_not_found", "Member no longer exists.")
        return target, target_user

    async def remove_member(
        self, principal: Principal, workspace_id: uuid.UUID, target_user_id: uuid.UUID
    ) -> None:
        workspace = await self._get_workspace(workspace_id)
        await self._lock_workspace(workspace.id)
        caller_user_id = _user_uuid(principal)

        if principal.key_id is None and target_user_id == caller_user_id:
            # Any active member may leave (but an API key is never a "self").
            membership = await self._get_membership(workspace.id, caller_user_id)
            if membership is None or membership.status != MembershipStatus.ACTIVE.value:
                raise ApiError(
                    403, "not_a_member", "You do not have access to this workspace."
                )
            caller_role = Role(membership.role)
        else:
            require(principal, Action.WORKSPACE_MANAGE, _resource(workspace.id))
            caller_role = principal.workspace_roles[str(workspace.id)]

        target = await self._require_membership(workspace.id, target_user_id)
        ensure_owner_can_manage_owner(caller_role, Role(target.role))

        owner_count = await self._count_owners(workspace.id)
        ensure_last_owner_safe(
            target_role=Role(target.role), new_role=None, owner_count=owner_count
        )
        await self._session.delete(target)
        await self._session.commit()

    # ---- helpers ---------------------------------------------------------

    async def _get_workspace(self, workspace_id: uuid.UUID) -> Workspace:
        workspace = await self._session.get(Workspace, workspace_id)
        if workspace is None:
            raise ApiError(404, "workspace_not_found", "Workspace not found.")
        return workspace

    async def _lock_workspace(self, workspace_id: uuid.UUID) -> None:
        """Serialize membership mutations on a workspace (M1-001d).

        A row lock on the workspace makes the owner count + mutation atomic, so
        two concurrent demote/remove-self requests cannot both pass the
        last-owner check and leave the workspace ownerless.
        """
        await self._session.execute(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
        )

    async def _require_membership(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> Membership:
        membership = await self._get_membership(workspace_id, user_id)
        if membership is None:
            raise ApiError(
                404, "member_not_found", "That user is not a member of this workspace."
            )
        return membership

    async def _get_membership(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> Membership | None:
        statement = select(Membership).where(
            Membership.workspace_id == workspace_id, Membership.user_id == user_id
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _count_owners(self, workspace_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.workspace_id == workspace_id,
                Membership.role == Role.OWNER.value,
                Membership.status == MembershipStatus.ACTIVE.value,
            )
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def _get_user_by_email(self, email: str) -> User | None:
        statement = select(User).where(User.email == email)
        return (await self._session.execute(statement)).scalar_one_or_none()


def _user_uuid(principal: Principal) -> uuid.UUID:
    try:
        return uuid.UUID(principal.user_id)
    except ValueError as exc:
        raise ApiError(401, "invalid_token", "Invalid principal.") from exc
