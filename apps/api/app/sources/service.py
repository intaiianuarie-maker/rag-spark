"""Connector source configuration service (M6-001c)."""

from __future__ import annotations

import uuid

from connectors import ConnectorConfig, ConnectorKind
from connectors.config_validation import validate_settings as _validate_settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.workspaces.models import Workspace

from .models import ConnectorConfigRecord
from .schemas import ConnectorCreate, ConnectorUpdate


class SourceConfigService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _workspace_exists(self, workspace_id: uuid.UUID) -> bool:
        row = await self._session.get(Workspace, workspace_id)
        return row is not None

    async def create(self, workspace_id: uuid.UUID, body: ConnectorCreate) -> ConnectorConfigRecord:
        settings = _validate_settings(body.kind, body.settings, workspace_id=str(workspace_id))
        if body.credential_ref is not None:
            raise ValueError("credential_ref is not supported by this adapter")
        record = ConnectorConfigRecord(
            workspace_id=workspace_id,
            connector_id=body.connector_id,
            kind=body.kind,
            settings=settings,
            credential_ref=body.credential_ref,
        )
        self._session.add(record)
        await self._session.commit()
        await self._session.refresh(record)
        return record

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[ConnectorConfigRecord]:
        stmt = (
            select(ConnectorConfigRecord)
            .where(ConnectorConfigRecord.workspace_id == workspace_id)
            .order_by(ConnectorConfigRecord.created_at, ConnectorConfigRecord.connector_id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, workspace_id: uuid.UUID, connector_id: str) -> ConnectorConfigRecord | None:
        stmt = select(ConnectorConfigRecord).where(
            ConnectorConfigRecord.workspace_id == workspace_id,
            ConnectorConfigRecord.connector_id == connector_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def update(
        self, workspace_id: uuid.UUID, connector_id: str, body: ConnectorUpdate
    ) -> ConnectorConfigRecord | None:
        record = await self.get(workspace_id, connector_id)
        if record is None:
            return None
        if body.settings is not None:
            record.settings = _validate_settings(
                record.kind, body.settings, workspace_id=str(workspace_id)
            )
        if "credential_ref" in body.model_fields_set:
            if body.credential_ref is not None:
                raise ValueError("credential_ref is not supported by this adapter")
            record.credential_ref = body.credential_ref
        await self._session.commit()
        await self._session.refresh(record)
        return record

    async def delete(self, workspace_id: uuid.UUID, connector_id: str) -> bool:
        record = await self.get(workspace_id, connector_id)
        if record is None:
            return False
        await self._session.delete(record)
        await self._session.commit()
        return True

    def to_connector_config(self, record: ConnectorConfigRecord) -> ConnectorConfig:
        return ConnectorConfig(
            connector_id=record.connector_id,
            workspace_id=str(record.workspace_id),
            kind=ConnectorKind(record.kind),
            settings=_validate_settings(
                record.kind, record.settings, workspace_id=str(record.workspace_id)
            ),
        )
