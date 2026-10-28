"""ModelConfig persistence service (TASK-M7-001a)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ModelConfig
from .schemas import ModelConfigCreate, ModelConfigUpdate


class ModelConfigService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        created_by: uuid.UUID | None,
        body: ModelConfigCreate,
    ) -> ModelConfig:
        cfg = ModelConfig(
            workspace_id=workspace_id,
            created_by=created_by,
            name=body.name,
            preferred_model=body.preferred_model,
            override_model=body.override_model,
            routing_rules=[r.model_dump() for r in body.routing_rules]
            if body.routing_rules
            else None,
            rate_limit_policy=body.rate_limit_policy,
            verify_uniform_cost=body.verify_uniform_cost,
            sensitive=body.sensitive,
            extra_models=body.extra_models,
        )
        self._session.add(cfg)
        await self._session.commit()
        await self._session.refresh(cfg)
        return cfg

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[ModelConfig]:
        stmt = (
            select(ModelConfig)
            .where(ModelConfig.workspace_id == workspace_id)
            .order_by(ModelConfig.created_at, ModelConfig.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, workspace_id: uuid.UUID, config_id: uuid.UUID) -> ModelConfig | None:
        stmt = select(ModelConfig).where(
            ModelConfig.id == config_id, ModelConfig.workspace_id == workspace_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def update(
        self, workspace_id: uuid.UUID, config_id: uuid.UUID, body: ModelConfigUpdate
    ) -> ModelConfig | None:
        cfg = await self.get(workspace_id, config_id)
        if cfg is None:
            return None
        if "preferred_model" in body.model_fields_set:
            cfg.preferred_model = body.preferred_model
        if "override_model" in body.model_fields_set:
            cfg.override_model = body.override_model
        if "routing_rules" in body.model_fields_set:
            cfg.routing_rules = (
                [r.model_dump() for r in body.routing_rules]
                if body.routing_rules is not None
                else None
            )
        if "rate_limit_policy" in body.model_fields_set and body.rate_limit_policy is not None:
            cfg.rate_limit_policy = body.rate_limit_policy
        if "verify_uniform_cost" in body.model_fields_set and body.verify_uniform_cost is not None:
            cfg.verify_uniform_cost = body.verify_uniform_cost
        if "sensitive" in body.model_fields_set and body.sensitive is not None:
            cfg.sensitive = body.sensitive
        if "extra_models" in body.model_fields_set:
            cfg.extra_models = body.extra_models
        await self._session.commit()
        await self._session.refresh(cfg)
        return cfg

    async def delete(self, workspace_id: uuid.UUID, config_id: uuid.UUID) -> bool:
        cfg = await self.get(workspace_id, config_id)
        if cfg is None:
            return False
        await self._session.delete(cfg)
        await self._session.commit()
        return True
