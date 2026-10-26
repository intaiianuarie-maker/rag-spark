"""Persisted sync source -> document identity mapping (M6-001b).

The sync publisher/deleter resolve a document through THIS mapping —
``(workspace_id, connector_id, external_id) -> document_id`` — never by
guessing a filename. Two properties POLICE's REQUEST-CHANGES required:

- a NESTED external id (``a/b/c.txt``) deletes exactly the document that
  source produced, whatever its display name;
- two independent connectors with the same basename keep separate documents
  (the identity is the full external id, not ``item.name``).

Identity is NEVER truncated (root review): the primary key carries a SHA-256
of the full ``external_id`` so two distinct long paths that share a 1024-char
prefix cannot alias onto one row, while the full id is stored verbatim as
evidence. Functions take the caller's session so the mapping row commits
atomically with the document/version/job it describes.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from connectors.adapters.progress_store import Base

#: Fixed-width key of the full external id (hex sha256) — the alias-proof
#: identity; the full id itself is kept verbatim in ``external_id``.
EXTERNAL_ID_KEY_WIDTH = 64
CONNECTOR_ID_MAX = 255


def external_id_key(external_id: str) -> str:
    """Alias-proof key of the FULL external id (never a truncation)."""
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()


class SyncSourceDocument(Base):
    """One sync-produced document, keyed by its source's full external id.

    Like the sync progress rows, this table carries no cross-package FK: the
    document reference is an indexed plain column, and the publisher/deleter
    keep the pair consistent inside one transaction (a purged document leaves
    at most a stale row the publisher replaces). Keeps the connectors package
    free of the ingestion metadata the ``documents`` FK would require.
    """

    __tablename__ = "sync_source_documents"

    workspace_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(CONNECTOR_ID_MAX), primary_key=True)
    #: SHA-256 of the FULL external id — the alias-proof identity.
    external_id_key: Mapped[str] = mapped_column(
        String(EXTERNAL_ID_KEY_WIDTH), primary_key=True
    )
    #: The full external id, verbatim (evidence; never a lookup key).
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``documents.id`` — plain column + index by design (see the class note).
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


async def get_mapped_document(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    connector_id: str,
    external_id: str,
) -> uuid.UUID | None:
    """The document this source item produced, or ``None`` when unmapped."""
    row = await session.get(
        SyncSourceDocument,
        (workspace_id, connector_id, external_id_key(external_id)),
    )
    return row.document_id if row is not None else None


async def put_mapping(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    connector_id: str,
    external_id: str,
    document_id: uuid.UUID,
    replace: bool = False,
) -> None:
    """Record the identity.

    Idempotent by default (an existing row is left untouched). ``replace``
    repoints the row — the path the publisher takes when the previously mapped
    document no longer exists (purged outside sync), so the stale reference
    cannot wedge the source.
    """
    key = external_id_key(external_id)
    existing = await session.get(SyncSourceDocument, (workspace_id, connector_id, key))
    if existing is not None:
        if replace:
            existing.document_id = document_id
        return
    session.add(
        SyncSourceDocument(
            workspace_id=workspace_id,
            connector_id=connector_id,
            external_id_key=key,
            external_id=external_id,
            document_id=document_id,
        )
    )


async def delete_mapping(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    connector_id: str,
    external_id: str,
) -> bool:
    """Drop the identity after its document was purged. True when a row went."""
    row = await session.get(
        SyncSourceDocument,
        (workspace_id, connector_id, external_id_key(external_id)),
    )
    if row is None:
        return False
    await session.delete(row)
    return True


async def mapped_document_ids(
    session: AsyncSession, *, workspace_id: uuid.UUID, connector_id: str
) -> set[uuid.UUID]:
    """Every document this connector currently owns (recovery evidence)."""
    rows = await session.scalars(
        select(SyncSourceDocument.document_id).where(
            SyncSourceDocument.workspace_id == workspace_id,
            SyncSourceDocument.connector_id == connector_id,
        )
    )
    return {row for row in rows}
