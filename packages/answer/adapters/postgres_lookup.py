"""Store-backed and in-memory chunk lookup adapters (RP-03 / QF-03).

Provides canonical chunk hydration to verify claims against authentic stored
passages rather than synthesized graph summaries or stale versions.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text

from ..interfaces import ChunkLookup
from ..models import Citation

logger = logging.getLogger("knowledge_os.answer.lookup")


class MemoryChunkLookup(ChunkLookup):
    """In-memory chunk lookup for tests and evaluation harnesses."""

    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        self._records = dict(records or {})

    def add_chunk(
        self,
        chunk_id: str,
        *,
        content: str,
        workspace_id: str,
        document_id: str | None = None,
        filename: str | None = None,
        page: int | None = None,
        section: str | None = None,
        is_deleted: bool = False,
        is_superseded: bool = False,
    ) -> None:
        self._records[chunk_id] = {
            "content": content,
            "workspace_id": workspace_id,
            "document_id": document_id,
            "filename": filename,
            "page": page,
            "section": section,
            "is_deleted": is_deleted,
            "is_superseded": is_superseded,
        }

    async def get(self, chunk_id: str) -> Citation | None:
        rec = self._records.get(chunk_id)
        if rec is None:
            return None
        if rec.get("is_deleted") or rec.get("is_superseded"):
            return None
        return Citation(
            chunk_id=chunk_id,
            document_id=rec.get("document_id"),
            filename=rec.get("filename"),
            page=rec.get("page"),
            section=rec.get("section"),
            workspace_id=rec.get("workspace_id"),
            content=rec.get("content"),
        )

    async def get_content(self, chunk_id: str) -> str | None:
        cit = await self.get(chunk_id)
        return cit.content if cit else None


class PostgresChunkLookup(ChunkLookup):
    """Authoritative chunk hydration backed by PostgreSQL chunks and documents tables."""

    def __init__(self, session_factory=None) -> None:
        if session_factory is None:
            from ingestion.persistence import get_session_factory

            self._session_factory = get_session_factory()
        else:
            self._session_factory = session_factory

    async def get(self, chunk_id: str) -> Citation | None:
        try:
            # Validate UUID shape
            UUID(chunk_id)
        except ValueError:
            return None

        stmt = text(
            """
            SELECT c.id, c.workspace_id, dv.document_id, c.document_version_id,
                   c.text, c.page, c.section, d.source_filename,
                   d.deleted_at, dv.superseded_at
            FROM chunks c
            JOIN document_versions dv ON c.document_version_id = dv.id
            JOIN documents d ON dv.document_id = d.id
            WHERE c.id = :chunk_id
            """
        )
        try:
            async with self._session_factory() as session:
                row = (await session.execute(stmt, {"chunk_id": chunk_id})).first()
                if row is None:
                    return None
                if row.deleted_at is not None or row.superseded_at is not None:
                    return None
                return Citation(
                    chunk_id=str(row.id),
                    document_id=str(row.document_id) if row.document_id else None,
                    filename=row.source_filename,
                    page=row.page,
                    section=row.section,
                    workspace_id=str(row.workspace_id) if row.workspace_id else None,
                    content=row.text,
                )
        except Exception as exc:
            logger.warning(
                "postgres_chunk_lookup_failed", extra={"chunk_id": chunk_id, "error": str(exc)}
            )
            return None

    async def get_content(self, chunk_id: str) -> str | None:
        cit = await self.get(chunk_id)
        return cit.content if cit else None
