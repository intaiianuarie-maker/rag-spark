"""Workspace-scoped persistent evaluation inputs and completed runs.

Only explicitly submitted golden inputs and score/configuration records are
stored. Retrieved document text, prompts and answers are never persisted here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.models import Base
from app.eval.models import EvaluationDataset, EvaluationRun


class DatasetRecord(Base):
    __tablename__ = "evaluation_datasets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunRecord(Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    dataset_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save_dataset(self, workspace_id: uuid.UUID, dataset: EvaluationDataset) -> None:
        if dataset.workspace_id != str(workspace_id):
            raise ValueError("dataset workspace mismatch")
        self.session.add(
            DatasetRecord(
                id=dataset.id, workspace_id=workspace_id, payload=dataset.model_dump(mode="json")
            )
        )
        await self.session.commit()

    async def get_dataset(
        self, workspace_id: uuid.UUID, dataset_id: str
    ) -> EvaluationDataset | None:
        row = (
            await self.session.execute(
                select(DatasetRecord).where(
                    DatasetRecord.workspace_id == workspace_id, DatasetRecord.id == dataset_id
                )
            )
        ).scalar_one_or_none()
        return EvaluationDataset.model_validate(row.payload) if row else None

    async def list_datasets(self, workspace_id: uuid.UUID) -> list[EvaluationDataset]:
        rows = (
            (
                await self.session.execute(
                    select(DatasetRecord)
                    .where(DatasetRecord.workspace_id == workspace_id)
                    .order_by(DatasetRecord.created_at, DatasetRecord.id)
                )
            )
            .scalars()
            .all()
        )
        return [EvaluationDataset.model_validate(row.payload) for row in rows]

    async def save_run(self, workspace_id: uuid.UUID, run: EvaluationRun) -> None:
        if await self.get_dataset(workspace_id, run.dataset_id) is None:
            raise ValueError("dataset not found in workspace")
        self.session.add(
            RunRecord(
                id=run.id,
                workspace_id=workspace_id,
                dataset_id=run.dataset_id,
                payload=run.model_dump(mode="json"),
            )
        )
        await self.session.commit()

    async def get_run(self, workspace_id: uuid.UUID, run_id: str) -> EvaluationRun | None:
        row = (
            await self.session.execute(
                select(RunRecord).where(
                    RunRecord.workspace_id == workspace_id, RunRecord.id == run_id
                )
            )
        ).scalar_one_or_none()
        return EvaluationRun.model_validate(row.payload) if row else None

    async def list_runs(self, workspace_id: uuid.UUID) -> list[EvaluationRun]:
        rows = (
            (
                await self.session.execute(
                    select(RunRecord)
                    .where(RunRecord.workspace_id == workspace_id)
                    .order_by(RunRecord.created_at, RunRecord.id)
                )
            )
            .scalars()
            .all()
        )
        return [EvaluationRun.model_validate(row.payload) for row in rows]
