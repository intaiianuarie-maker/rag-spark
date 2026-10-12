"""Document delete endpoint (M2-001d / V1-Q3): tombstone + purge.

Postgres chunks are removed and the document is tombstoned (`deleted_at`).
Qdrant/FTS purges run through `ingestion.dedup` store purgers with a
document-scoped filter (`build_purge_filter`); the delete endpoint reports the
per-store report and always returns 204 once the tombstone commits.
`GET .../documents/{id}` on a tombstoned document returns an honest 404
(`document_not_found`), and deleted chunks are gone from FTS/Qdrant so search
and ask can never return or cite them.
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
