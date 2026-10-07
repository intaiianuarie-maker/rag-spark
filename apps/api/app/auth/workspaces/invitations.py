"""My-invitations endpoint (M1-001f frozen contract).

`GET /v1/me/invitations` -> 200 [{workspace_id, workspace_name, role, invited_at}]
for the caller's own pending (status=invited) memberships only.
"""

from __future__ import annotations

from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.auth.workspaces.schemas import InvitationItem, WorkspaceErrorBody
from app.auth.workspaces.service import WorkspaceService
from app.authz import ApiError
from app.authz.principal import get_principal

router = APIRouter(prefix="/v1/me", tags=["invitations"])

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": WorkspaceErrorBody, "description": "Missing/invalid credential."},
}


def _http_error(exc: ApiError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    )


@router.get(
    "/invitations",
    response_model=list[InvitationItem],
    responses=_ERROR_RESPONSES,
    summary="List the caller's own pending workspace invitations (M1-001f)",
)
async def list_my_invitations(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[InvitationItem]:
    service = WorkspaceService(session)
    try:
        invitations = await service.list_invitations(principal)
    except ApiError as exc:
        raise _http_error(exc) from exc
    return [
        InvitationItem(
            workspace_id=str(workspace_id),
            workspace_name=workspace_name,
            role=role,
            invited_at=invited_at,
        )
        for workspace_id, workspace_name, role, invited_at in invitations
    ]
