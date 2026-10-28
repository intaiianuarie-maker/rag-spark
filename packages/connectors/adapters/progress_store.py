"""SQLAlchemy adapter for the T-V1-034 sync progress store.

Vendor DB access lives here (``/adapters/`` is the CI vendor fence);
``connectors.sync_consumer`` only ever sees the ``SyncProgressStore`` protocol.

Tables (migration ``0008_connector_sync_progress``, hand-written like the rest
of the chain — ``alembic/env.py`` carries no autogenerate metadata):

- ``connector_sync_progress`` — one row per source: persisted **source
  identity** (workspace + connector + kind + settings fingerprint),
  generation, the **keyset cursor** (never an offset), the listing **snapshot**
  digest and the run's item counters;
- ``connector_sync_outcomes`` — one row per item per generation: status,
  **checksum**, failure code/message. That is what lets the next run say
  ``created``/``updated``/``unchanged`` without rescanning anything.

Both upsert on their natural key, so a redelivered job is idempotent
(T-V1-020 at-least-once delivery + T-V1-016 idempotent publication).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from ingestion.persistence import get_session_factory as get_ingest_session_factory
from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, delete, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..interfaces import ConnectorKind
from ..sync_consumer import ItemOutcome, ItemStatus, SourceIdentity, SyncProgress


class Base(DeclarativeBase):
    """Declarative base for connector sync persistence."""


class SyncProgressRow(Base):
    """Persisted progress for one synced source (T-V1-034)."""

    __tablename__ = "connector_sync_progress"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "connector_id", name="uq_connector_sync_progress_source"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    connector_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    settings_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Keyset cursor (``k:<external_id>``): TEXT, S3 keys and URLs are long.
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    items_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Durable source-level failure markers (root review): a discovery
    #: failure happens BEFORE any ItemOutcome, so without these the
    #: status endpoint cannot see an unreachable host / bad credentials.
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SyncOutcomeRow(Base):
    """One item's persisted outcome (status + checksum + failure code)."""

    __tablename__ = "connector_sync_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "connector_id",
            "generation",
            "external_id",
            name="uq_connector_sync_outcomes_item",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    connector_id: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SqlAlchemySyncProgressStore:
    """Durable ``SyncProgressStore`` on Postgres.

    Reuses the metadata-DB session factory that ``ingestion.persistence``
    owns (one database, one engine/pool policy) so the connector package does
    not open a second pool to the same server; tests inject their own
    ``session_factory`` instead.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        # ``async_sessionmaker`` is itself a 0-arg session factory, so the
        # default is adapted (not re-created) to the same callable type.
        self._session_factory: Callable[[], AsyncSession] = session_factory or (
            lambda: get_ingest_session_factory()()
        )

    @staticmethod
    def _identity_of(row: SyncProgressRow) -> SourceIdentity:
        return SourceIdentity(
            workspace_id=row.workspace_id,
            connector_id=row.connector_id,
            kind=ConnectorKind(row.kind),
            settings_fingerprint=row.settings_fingerprint,
        )

    @classmethod
    def _progress_of(cls, row: SyncProgressRow) -> SyncProgress:
        return SyncProgress(
            identity=cls._identity_of(row),
            generation=row.generation,
            cursor=row.cursor,
            snapshot=row.snapshot,
            last_run_id=row.last_run_id,
            items_seen=row.items_seen,
            items_created=row.items_created,
            items_updated=row.items_updated,
            items_unchanged=row.items_unchanged,
            items_failed=row.items_failed,
            items_deleted=row.items_deleted,
            last_attempt_at=row.last_attempt_at,
            last_success_at=row.last_success_at,
            last_error_code=row.last_error_code,
            last_finished_at=row.last_finished_at,
            updated_at=row.updated_at,
        )

    async def load(self, identity: SourceIdentity) -> SyncProgress | None:
        async with self._session_factory() as session:
            row = await session.scalar(self._progress_stmt(identity))
        return self._progress_of(row) if row is not None else None

    async def save(self, progress: SyncProgress) -> None:
        identity = progress.identity
        async with self._session_factory() as session:
            row = await session.scalar(self._progress_stmt(identity))
            if row is None:
                row = SyncProgressRow(
                    workspace_id=identity.workspace_id,
                    connector_id=identity.connector_id,
                    kind=str(identity.kind),
                    settings_fingerprint=identity.settings_fingerprint,
                )
                session.add(row)
            row.kind = str(identity.kind)
            row.settings_fingerprint = identity.settings_fingerprint
            row.generation = progress.generation
            row.cursor = progress.cursor
            row.snapshot = progress.snapshot
            row.last_run_id = progress.last_run_id
            row.items_seen = progress.items_seen
            row.items_created = progress.items_created
            row.items_updated = progress.items_updated
            row.items_unchanged = progress.items_unchanged
            row.items_failed = progress.items_failed
            row.items_deleted = progress.items_deleted
            row.last_attempt_at = progress.last_attempt_at
            row.last_success_at = progress.last_success_at
            row.last_error_code = progress.last_error_code
            row.last_finished_at = progress.last_finished_at
            await session.commit()

    async def record_outcome(self, identity: SourceIdentity, outcome: ItemOutcome) -> None:
        """Upsert one item's outcome (idempotent under redelivery)."""
        values = {
            "workspace_id": identity.workspace_id,
            "connector_id": identity.connector_id,
            "generation": outcome.generation,
            "external_id": outcome.external_id,
            "status": str(outcome.status),
            "checksum": outcome.checksum,
            "size_bytes": outcome.size_bytes,
            "error_code": outcome.error_code,
            "error_message": outcome.error_message,
            "run_id": outcome.run_id,
            "occurred_at": outcome.occurred_at or datetime.now(UTC),
        }
        stmt = pg_insert(SyncOutcomeRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "workspace_id",
                "connector_id",
                "generation",
                "external_id",
            ],
            set_={
                "status": stmt.excluded.status,
                "checksum": stmt.excluded.checksum,
                "size_bytes": stmt.excluded.size_bytes,
                "error_code": stmt.excluded.error_code,
                "error_message": stmt.excluded.error_message,
                "run_id": stmt.excluded.run_id,
                "occurred_at": stmt.excluded.occurred_at,
            },
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def outcomes(
        self, identity: SourceIdentity, generation: int
    ) -> dict[str, ItemOutcome]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(SyncOutcomeRow).where(
                        SyncOutcomeRow.workspace_id == identity.workspace_id,
                        SyncOutcomeRow.connector_id == identity.connector_id,
                        SyncOutcomeRow.generation == generation,
                    )
                )
            ).all()
        return {
            row.external_id: ItemOutcome(
                external_id=row.external_id,
                status=ItemStatus(row.status),
                checksum=row.checksum,
                size_bytes=row.size_bytes,
                error_code=row.error_code,
                error_message=row.error_message,
                run_id=row.run_id,
                occurred_at=row.occurred_at,
                generation=row.generation,
            )
            for row in rows
        }

    async def reset(self, identity: SourceIdentity) -> None:
        """Drop progress + outcomes for a reconfigured source (new generation)."""
        async with self._session_factory() as session:
            await session.execute(
                delete(SyncProgressRow).where(
                    SyncProgressRow.workspace_id == identity.workspace_id,
                    SyncProgressRow.connector_id == identity.connector_id,
                )
            )
            await session.execute(
                delete(SyncOutcomeRow).where(
                    SyncOutcomeRow.workspace_id == identity.workspace_id,
                    SyncOutcomeRow.connector_id == identity.connector_id,
                )
            )
            await session.commit()

    @staticmethod
    def _progress_stmt(identity: SourceIdentity):
        return select(SyncProgressRow).where(
            SyncProgressRow.workspace_id == identity.workspace_id,
            SyncProgressRow.connector_id == identity.connector_id,
        )


__all__ = [
    "Base",
    "SqlAlchemySyncProgressStore",
    "SyncOutcomeRow",
    "SyncProgressRow",
]
