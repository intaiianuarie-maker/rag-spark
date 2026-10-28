"""Canonical citation passages: authorized, workspace-scoped current SQL rows."""

from __future__ import annotations

import math
import uuid

from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException
from ingestion.persistence import ChunkRow, Document, DocumentVersion
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.authz.principal import get_principal
from app.documents.reads import _require_read

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/chunks", tags=["knowledge"])


class CanonicalPassage(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    workspace_id: uuid.UUID
    filename: str | None = None
    page: int | None = None
    section: str | None = None
    content: str = Field(min_length=1)
    span: tuple[int, int] | None = None
    bbox: tuple[float, float, float, float] | None = None


def _coordinates(metadata: dict, name: str, length: int) -> tuple | None:
    value = metadata.get(name)
    if (isinstance(value, (list, tuple)) and len(value) == length
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) for v in value)):
        return tuple(value)
    return None


def _span(metadata: dict, content: str) -> tuple[int, int] | None:
    value = metadata.get("span")
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
            and 0 <= value[0] <= value[1] <= len(content)):
        return value[0], value[1]
    return None


@router.get("/{chunk_id}", response_model=CanonicalPassage)
async def get_chunk(
    workspace_id: uuid.UUID,
    chunk_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> CanonicalPassage:
    # The SQL statement must not execute until DOCUMENT_READ is authorized.
    _require_read(principal, workspace_id)
    statement = (
        select(ChunkRow, Document.id, Document.source_filename)
        .join(DocumentVersion, ChunkRow.document_version_id == DocumentVersion.id)
        .join(Document, DocumentVersion.document_id == Document.id)
        .where(
            ChunkRow.id == chunk_id,
            ChunkRow.workspace_id == workspace_id,
            Document.workspace_id == workspace_id,
            Document.current_version_id == DocumentVersion.id,
            Document.deleted_at.is_(None),
            DocumentVersion.superseded_at.is_(None),
            ChunkRow.text != "",
        )
    )
    try:
        row = (await session.execute(statement)).one_or_none()
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail={"error": "canonical_store_unavailable", "message": "Passage unavailable."},
        ) from None
    if row is None or not row[0].text.strip():
        raise HTTPException(
            status_code=404,
            detail={"error": "chunk_not_found", "message": "Passage not found."},
        )
    chunk, document_id, filename = row
    metadata = chunk.chunk_metadata if isinstance(chunk.chunk_metadata, dict) else {}
    return CanonicalPassage(
        chunk_id=chunk.id,
        document_id=document_id,
        workspace_id=chunk.workspace_id,
        filename=filename,
        page=chunk.page,
        section=chunk.section,
        content=chunk.text,
        span=_span(metadata, chunk.text),
        bbox=_coordinates(metadata, "bbox", 4),
    )
