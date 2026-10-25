"""ModelConfig HTTP routes (TASK-M7-001a)."""

from __future__ import annotations

import uuid

from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal
from app.models_config.models import ModelConfig
from app.models_config.schemas import (
    ModelConfigCreate,
    ModelConfigPublic,
    ModelConfigUpdate,
)
from app.models_config.service import ModelConfigService

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/model-configs", tags=["model-configs"])

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"description": "Missing/invalid credential."},
    403: {"description": "Insufficient role."},
    404: {"description": "Model config not found."},
    422: {"description": "Validation error."},
}


def _http_error(exc: ApiError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}
    )


def _public(cfg: ModelConfig) -> ModelConfigPublic:
    return ModelConfigPublic(
        id=str(cfg.id),
        workspace_id=str(cfg.workspace_id),
        name=cfg.name,
        preferred_model=cfg.preferred_model,
        override_model=cfg.override_model,
        routing_rules=cfg.routing_rules,
        rate_limit_policy=cfg.rate_limit_policy,
        verify_uniform_cost=cfg.verify_uniform_cost,
        sensitive=cfg.sensitive,
        extra_models=cfg.extra_models,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ModelConfigPublic,
    responses=_ERROR_RESPONSES,
    summary="Create a workspace ModelConfig",
)
async def create_config(
    workspace_id: uuid.UUID,
    body: ModelConfigCreate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> ModelConfigPublic:
    try:
        require(principal, Action.WORKSPACE_MANAGE, Resource(workspace_id=str(workspace_id)))
        cfg = await ModelConfigService(session).create(
            workspace_id=workspace_id,
            created_by=uuid.UUID(principal.user_id) if principal.user_id else None,
            body=body,
        )
    except ApiError as exc:
        raise _http_error(exc) from exc
    return _public(cfg)


@router.get(
    "",
    response_model=list[ModelConfigPublic],
    responses=_ERROR_RESPONSES,
    summary="List workspace ModelConfigs",
)
async def list_configs(
    workspace_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[ModelConfigPublic]:
    try:
        require(principal, Action.WORKSPACE_READ, Resource(workspace_id=str(workspace_id)))
        cfgs = await ModelConfigService(session).list_for_workspace(workspace_id)
    except ApiError as exc:
        raise _http_error(exc) from exc
    return [_public(c) for c in cfgs]


@router.get(
    "/{config_id}",
    response_model=ModelConfigPublic,
    responses=_ERROR_RESPONSES,
    summary="Get a workspace ModelConfig",
)
async def get_config(
    workspace_id: uuid.UUID,
    config_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> ModelConfigPublic:
    try:
        require(principal, Action.WORKSPACE_READ, Resource(workspace_id=str(workspace_id)))
        cfg = await ModelConfigService(session).get(workspace_id, config_id)
    except ApiError as exc:
        raise _http_error(exc) from exc
    if cfg is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "model config not found"}
        )
    return _public(cfg)


@router.patch(
    "/{config_id}",
    response_model=ModelConfigPublic,
    responses=_ERROR_RESPONSES,
    summary="Update a workspace ModelConfig",
)
async def update_config(
    workspace_id: uuid.UUID,
    config_id: uuid.UUID,
    body: ModelConfigUpdate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> ModelConfigPublic:
    try:
        require(principal, Action.WORKSPACE_MANAGE, Resource(workspace_id=str(workspace_id)))
        cfg = await ModelConfigService(session).update(workspace_id, config_id, body)
    except ApiError as exc:
        raise _http_error(exc) from exc
    if cfg is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "model config not found"}
        )
    return _public(cfg)


@router.delete(
    "/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERROR_RESPONSES,
    summary="Delete a workspace ModelConfig",
)
async def delete_config(
    workspace_id: uuid.UUID,
    config_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> None:
    try:
        require(principal, Action.WORKSPACE_MANAGE, Resource(workspace_id=str(workspace_id)))
        ok = await ModelConfigService(session).delete(workspace_id, config_id)
    except ApiError as exc:
        raise _http_error(exc) from exc
    if not ok:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "model config not found"}
        )
