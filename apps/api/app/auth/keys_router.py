"""API-key HTTP routes (M1-001c): ``/v1/workspaces/{workspace_id}/keys``.

Issue / list / revoke, authorized via ``authz.can`` (``key.manage``). The full
secret is returned exactly once on creation; list/revoke only ever expose the
prefix and metadata.
"""

from __future__ import annotations

import uuid

from core.auth import Principal, Role
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.auth.keys import (
    ApiKey,
    ApiKeyCreated,
    ApiKeyCreateRequest,
    ApiKeyPublic,
    KeyService,
)
from app.auth.workspaces.schemas import WorkspaceErrorBody
from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/keys", tags=["api-keys"]
)

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": WorkspaceErrorBody, "description": "Missing/invalid credential."},
    403: {"model": WorkspaceErrorBody, "description": "Insufficient role."},
    404: {"model": WorkspaceErrorBody, "description": "Key not found."},
    422: {"description": "Validation error (input redacted)."},
}


def _http_error(exc: ApiError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    )


def _public(key: ApiKey) -> ApiKeyPublic:
    return ApiKeyPublic(
        id=str(key.id),
        name=key.name,
        prefix=key.prefix,
        role=Role(key.role),
        created_at=key.created_at,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
    )


def _created(key: ApiKey, full_key: str) -> ApiKeyCreated:
    return ApiKeyCreated(
        id=str(key.id),
        name=key.name,
        prefix=key.prefix,
        role=Role(key.role),
        created_at=key.created_at,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        key=full_key,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeyCreated,
    responses=_ERROR_RESPONSES,
    summary="Issue a workspace API key (secret returned once)",
)
async def create_key(
    workspace_id: uuid.UUID,
    body: ApiKeyCreateRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreated:
    try:
        require(principal, Action.KEY_MANAGE, Resource(workspace_id=str(workspace_id)))
        service = KeyService(session)
        key, full_key = await service.create(
            workspace_id=workspace_id,
            created_by=uuid.UUID(principal.user_id),
            name=body.name,
            role=body.role,
            expires_at=body.expires_at,
        )
    except ApiError as exc:
        raise _http_error(exc) from exc
    return _created(key, full_key)


@router.get(
    "",
    response_model=list[ApiKeyPublic],
    responses=_ERROR_RESPONSES,
    summary="List workspace API keys (prefix + metadata only)",
)
async def list_keys(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[ApiKeyPublic]:
    try:
        require(principal, Action.KEY_MANAGE, Resource(workspace_id=str(workspace_id)))
        keys = await KeyService(session).list_for_workspace(workspace_id)
    except ApiError as exc:
        raise _http_error(exc) from exc
    return [_public(key) for key in keys]


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_ERROR_RESPONSES,
    summary="Revoke a workspace API key",
)
async def revoke_key(
    workspace_id: uuid.UUID,
    key_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        require(principal, Action.KEY_MANAGE, Resource(workspace_id=str(workspace_id)))
        key = await KeyService(session).revoke(workspace_id, key_id)
        if key is None:
            raise ApiError(404, "key_not_found", "API key not found.")
    except ApiError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
