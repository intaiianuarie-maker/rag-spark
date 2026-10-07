"""Document delete endpoint (M2-001d): tombstone + purge.

Postgres chunks are removed and the document is tombstoned. Qdrant/FTS purge is
registered by M3 adapters (`ingestion.dedup.register_store_purger`); until then
the per-store report shows them not purged. Original bytes stay in Postgres
until PD-005 moves them to MinIO.
"""

from __future__ import annotations

import logging
import uuid

from core.auth import Principal
from fastapi import APIRouter, Depends, HTTPException, Response, status
from ingestion.dedup import purge_document
from ingestion.persistence import Document
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db import get_session
from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal

logger = logging.getLogger("knowledge_os.api.documents")

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/documents", tags=["documents"])


def _http_error(exc: ApiError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Tombstone a document and purge its derived rows",
)
async def delete_document(
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        require(principal, Action.DOCUMENT_WRITE, Resource(workspace_id=str(workspace_id)))
    except ApiError as exc:
        raise _http_error(exc) from exc

    document = await session.get(Document, document_id)
    if document is None or document.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "document_not_found", "message": "Document not found."},
        )

    report = await purge_document(
        session, workspace_id=workspace_id, document_id=document_id
    )
    logger.info(
        "document_deleted",
        extra={
            "event": "document_deleted",
            "document_id": str(document_id),
            "workspace_id": str(workspace_id),
            "stores": report,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
