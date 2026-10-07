"""Workspace HTTP routes (M1-001b/M1-001c): ``/v1/workspaces``.

Route handlers only call the service and serialize — authorization is entirely
inside ``app.authz`` (the single ``can()`` path). Errors use the app-wide nested
shape ``{"detail": {"error", "message"}}``; 422 is the global lean shape.
"""

from __future__ import annotations

import uuid

from core.auth import Principal, Role
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.auth.models import User
from app.auth.workspaces.models import Membership, MembershipStatus, Workspace
from app.auth.workspaces.schemas import (
    MemberInviteRequest,
    MemberPublic,
    MemberRoleUpdateRequest,
    WorkspaceCreateRequest,
    WorkspaceErrorBody,
    WorkspacePublic,
    WorkspaceRenameRequest,
)
from app.auth.workspaces.service import WorkspaceService
from app.authz import ApiError
from app.authz.principal import get_principal

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": WorkspaceErrorBody, "description": "Missing/invalid credential."},
    403: {"model": WorkspaceErrorBody, "description": "Not a member / insufficient role."},
    404: {"model": WorkspaceErrorBody, "description": "Workspace or user not found."},
    409: {"model": WorkspaceErrorBody, "description": "Conflict (already member / last owner)."},
    422: {"description": "Validation error (input redacted)."},
}


def _http_error(exc: ApiError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    )


def _workspace_public(workspace: Workspace, role: Role) -> WorkspacePublic:
    return WorkspacePublic(
        id=str(workspace.id),
        name=workspace.name,
        organization_id=str(workspace.organization_id),
        role=role,
        created_at=workspace.created_at,
    )


def _member_public(membership: Membership, user: User) -> MemberPublic:
    return MemberPublic(
        user_id=str(membership.user_id),
        email=user.email,
        role=Role(membership.role),
        status=MembershipStatus(membership.status),
        created_at=membership.created_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=WorkspacePublic,
    responses=_ERROR_RESPONSES,
    summary="Create a workspace (caller becomes owner)",
)
async def create_workspace(
    body: WorkspaceCreateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> WorkspacePublic:
    service = WorkspaceService(session)
    try:
        workspace, role = await service.create_workspace(
            principal, body.name, body.organization_name
        )
    except ApiError as exc:
        raise _http_error(exc) from exc
    return _workspace_public(workspace, role)


@router.get(
    "",
    response_model=list[WorkspacePublic],
    responses=_ERROR_RESPONSES,
    summary="List workspaces the caller is an active member of",
)
async def list_workspaces(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[WorkspacePublic]:
    service = WorkspaceService(session)
    try:
        workspaces = await service.list_workspaces(principal)
    except ApiError as exc:
        raise _http_error(exc) from exc
    return [_workspace_public(workspace, role) for workspace, role in workspaces]


@router.get(
    "/{workspace_id}",
    response_model=WorkspacePublic,
    responses=_ERROR_RESPONSES,
    summary="Get a workspace (members only)",
)
async def get_workspace(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> WorkspacePublic:
    service = WorkspaceService(session)
    try:
        workspace, role = await service.get_workspace(principal, workspace_id)
    except ApiError as exc:
        raise _http_error(exc) from exc
    return _workspace_public(workspace, role)


@router.patch(
    "/{workspace_id}",
    response_model=WorkspacePublic,
    responses=_ERROR_RESPONSES,
    summary="Rename a workspace (owner/admin)",
)
async def rename_workspace(
    workspace_id: uuid.UUID,
    body: WorkspaceRenameRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> WorkspacePublic:
    service = WorkspaceService(session)
    try:
        workspace, role = await service.rename_workspace(principal, workspace_id, body.name)
    except ApiError as exc:
        raise _http_error(exc) from exc
    return _workspace_public(workspace, role)


@router.post(
    "/{workspace_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=MemberPublic,
    responses=_ERROR_RESPONSES,
    summary="Invite a user (owner/admin)",
)
async def invite_member(
    workspace_id: uuid.UUID,
    body: MemberInviteRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> MemberPublic:
    service = WorkspaceService(session)
    try:
        membership, target = await service.invite_member(
            principal, workspace_id, body.email, body.role
        )
    except ApiError as exc:
        raise _http_error(exc) from exc
    return _member_public(membership, target)


@router.post(
    "/{workspace_id}/members/accept",
    response_model=MemberPublic,
    responses=_ERROR_RESPONSES,
    summary="Accept a pending invitation (invited user)",
)
async def accept_invite(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> MemberPublic:
    service = WorkspaceService(session)
    try:
        membership, target = await service.accept_invite(principal, workspace_id)
    except ApiError as exc:
        raise _http_error(exc) from exc
    return _member_public(membership, target)


@router.patch(
    "/{workspace_id}/members/{user_id}",
    response_model=MemberPublic,
    responses=_ERROR_RESPONSES,
    summary="Change a member's role (owner/admin; last-owner guard)",
)
async def change_member_role(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberRoleUpdateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> MemberPublic:
    service = WorkspaceService(session)
    try:
        membership, target = await service.change_role(
            principal, workspace_id, user_id, body.role
        )
    except ApiError as exc:
        raise _http_error(exc) from exc
    return _member_public(membership, target)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_ERROR_RESPONSES,
    summary="Remove a member, or leave (last-owner guard)",
)
async def remove_member(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    service = WorkspaceService(session)
    try:
        await service.remove_member(principal, workspace_id, user_id)
    except ApiError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
