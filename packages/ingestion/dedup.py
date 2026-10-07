"""Checksum dedup, immutable versions, delete tombstones (M2-001d).

Postgres is fully handled here. Vector (Qdrant) and full-text (FTS) purge land
in M3-001a behind the store interfaces; this module exposes the registration
seam and reports which stores were purged. Original bytes remain in
``document_versions.content`` until PD-005 moves them to MinIO.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ingestion.persistence import ChunkRow, Document, DocumentVersion, IngestionJob

StorePurger = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]

#: Store purgers registered by later adapters (M3-001a: qdrant/fts).
_STORE_PURGERS: dict[str, StorePurger] = {}


def register_store_purger(name: str, purger: StorePurger) -> None:
    """Register a downstream store purge (M3 seam)."""
    _STORE_PURGERS[name] = purger


def registered_stores() -> tuple[str, ...]:
    return tuple(sorted(_STORE_PURGERS))


async def find_document_by_filename(
    session: AsyncSession, workspace_id: uuid.UUID, filename: str | None
) -> Document | None:
    """Find a live document by (workspace, source filename) for dedup/versioning."""
    if not filename:
        return None
    statement = (
        select(Document)
        .where(
            Document.workspace_id == workspace_id,
            Document.source_filename == filename,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc())
    )
    return (await session.execute(statement)).scalars().first()


async def latest_version(
    session: AsyncSession, document_id: uuid.UUID
) -> DocumentVersion | None:
    statement = (
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
    )
    return (await session.execute(statement)).scalars().first()


async def latest_job_for_version(
    session: AsyncSession, version_id: uuid.UUID
) -> IngestionJob | None:
    statement = (
        select(IngestionJob)
        .where(IngestionJob.document_version_id == version_id)
        .order_by(IngestionJob.created_at.desc())
    )
    return (await session.execute(statement)).scalars().first()


async def supersede_other_versions(
    session: AsyncSession, document_id: uuid.UUID, keep_version_id: uuid.UUID
) -> int:
    """Supersede **strictly older** versions only (never clobber a newer one).

    A retryable failure can park an older version's job while a newer version
    completes first; when the older retry finishes, it must not delete the newer
    version's chunks.
    """
    keep = await session.get(DocumentVersion, keep_version_id)
    if keep is None:
        return 0
    older = (
        (
            await session.execute(
                select(DocumentVersion.id).where(
                    DocumentVersion.document_id == document_id,
                    DocumentVersion.version_number < keep.version_number,
                )
            )
        )
        .scalars()
        .all()
    )
    if not older:
        return 0
    await session.execute(delete(ChunkRow).where(ChunkRow.document_version_id.in_(older)))
    await session.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id.in_(older))
        .values(superseded_at=datetime.now(UTC))
    )
    return len(older)


async def purge_document(
    session: AsyncSession, *, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> dict[str, bool]:
    """Tombstone a document and remove its rows; returns a per-store report."""
    document = await session.get(Document, document_id)
    if document is None or document.workspace_id != workspace_id:
        return {}

    version_ids = (
        (
            await session.execute(
                select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
            )
        )
        .scalars()
        .all()
    )
    if version_ids:
        await session.execute(delete(ChunkRow).where(ChunkRow.document_version_id.in_(version_ids)))
    document.deleted_at = datetime.now(UTC)
    await session.commit()

    report: dict[str, bool] = {"postgres": True}
    for name, purger in _STORE_PURGERS.items():
        try:
            await purger(workspace_id, document_id)
            report[name] = True
        except Exception:
            report[name] = False
    return report
