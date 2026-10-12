"""Checksum dedup, immutable versions, delete tombstones (M2-001d).

Postgres is fully handled here. Vector (Qdrant) and full-text (FTS) purge land
in M3-001a behind the store interfaces; this module exposes the registration
seam and reports which stores were purged. Original bytes remain in
``document_versions.content`` until PD-005 moves them to MinIO.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ingestion.persistence import ChunkRow, Document, DocumentVersion, IngestionJob

if TYPE_CHECKING:
    from retrieval.interfaces import SearchFilter

logger = logging.getLogger("knowledge_os.ingestion.dedup")

StorePurger = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]

#: Store purgers registered by later adapters (M3-001a: qdrant/fts).
_STORE_PURGERS: dict[str, StorePurger] = {}


def build_purge_filter(workspace_id: uuid.UUID, document_id: uuid.UUID) -> SearchFilter:
    """Q3 tombstone filter: scope a purge to one document in one workspace.

    Factored out so the filter shape (``document_ids=[str(document_id)]`` and
    ``workspace_ids=[str(workspace_id)]``) is unit-testable without live
    Qdrant/Postgres; both default store purgers must use this exact filter.
    """
    from retrieval.interfaces import SearchFilter

    return SearchFilter(
        workspace_ids=[str(workspace_id)],
        document_ids=[str(document_id)],
    )


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


async def latest_version(session: AsyncSession, document_id: uuid.UUID) -> DocumentVersion | None:
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
    if not _STORE_PURGERS:
        # Default lazy registration of real store purgers (TASK-Q3 / M3-001a)
        try:
            from retrieval.adapters.pg_fts import PostgresLexicalStore
            from retrieval.adapters.qdrant_store import QdrantVectorStore

            async def _purge_fts(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
                store = PostgresLexicalStore()
                filt = build_purge_filter(ws_id, doc_id)
                await store.delete(query_filter=filt)

            async def _purge_qdrant(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
                qdrant = QdrantVectorStore(collection="knowledge")
                filt = build_purge_filter(ws_id, doc_id)
                await qdrant.delete(collection="knowledge", query_filter=filt)
                await qdrant.close()

            register_store_purger("fts", _purge_fts)
            register_store_purger("qdrant", _purge_qdrant)
        except Exception as exc:
            # Registration failure must never read as a clean purge: the
            # vector/lexical stores were NOT purged, so mark them failed in
            # the per-store report (review1 F2 — no false 204).
            logger.warning(
                "store_purger_registration_failed",
                extra={
                    "event": "store_purger_registration_failed",
                    "workspace_id": str(workspace_id),
                    "document_id": str(document_id),
                    "error": str(exc),
                },
            )
            report.setdefault("fts", False)
            report.setdefault("qdrant", False)

    # T-V1-007 (RP-04-lite, QF-04): the GRAPH_MODE check is re-evaluated on
    # EVERY call, not only when the registry is empty, so a late
    # ``GRAPH_MODE=rule`` toggle still purges graph rows on the next delete
    # (the registry is non-empty from the first purge onwards, which used to
    # freeze the graph decision made at that moment). Registration is keyed
    # by name, so re-registering is idempotent — exactly one purge per store
    # per call, never a double-purge. Toggling the mode back off unregisters
    # the purger again so a stale closure never fires.
    try:
        if os.environ.get("GRAPH_MODE", "off").strip().lower() in ("rule", "llm"):
            from graph.factory import graph_db_path
            from graph.sqlite_store import SQLiteGraphStore

            async def _purge_graph(ws_id: uuid.UUID, doc_id: uuid.UUID) -> None:
                gstore = SQLiteGraphStore(graph_db_path())
                await gstore.tombstone_document(str(ws_id), str(doc_id))

            register_store_purger("graph", _purge_graph)
        else:
            _STORE_PURGERS.pop("graph", None)
    except Exception as exc:
        # Same no-false-204 rule as the fts/qdrant seam: a graph purge that
        # cannot even be registered is reported as a failed store, never
        # silently dropped from the per-store report.
        logger.warning(
            "graph_purger_registration_failed",
            extra={
                "event": "graph_purger_registration_failed",
                "workspace_id": str(workspace_id),
                "document_id": str(document_id),
                "error": str(exc),
            },
        )
        report.setdefault("graph", False)

    for name, purger in _STORE_PURGERS.items():
        try:
            await purger(workspace_id, document_id)
            report[name] = True
        except Exception as exc:
            logger.warning(
                "store_purge_failed",
                extra={
                    "event": "store_purge_failed",
                    "store": name,
                    "workspace_id": str(workspace_id),
                    "document_id": str(document_id),
                    "error": str(exc),
                },
            )
            report[name] = False
    return report
