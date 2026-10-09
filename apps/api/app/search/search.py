"""POST /v1/workspaces/{workspace_id}/search — thin search endpoint (TASK-M5-002a).

Thin HTTP over the accepted M3-004 orchestrator: authenticate + authorize
(workspace read, same ACL pattern as ``app/ask/router.py``), map the typed
request to the frozen orchestrator ``retrieve()`` call, and shape the ranked
items. NO new retrieval logic, NO parallel retrieval path.

Mode selector: keyword (lexical-only M3-003 FTS), semantic (dense-only M3-001
Qdrant), hybrid DEFAULT (existing RRF fusion, unchanged).

Filters: ``type``/``source``/``collection`` ride in ``SearchFilter.metadata``
(both stores apply ``metadata`` pre-ranking — see ``pg_fts.py``/``qdrant_store.py``
``_filter_from_search_filter``); ``language`` uses ``SearchFilter.language``
(native column/payload pre-ranking). date is a string predicate
(YYYY-MM-DD exact or start..end range) matched
against the ``created_at``/``updated_at``/``date`` metadata keys. A provided
filter that matches nothing narrows (never widens): ACL scope is unchanged and
filters only remove rows.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from core.auth import ACLScope, Principal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from retrieval.interfaces import RetrievalMode, SearchFilter

from app.authz import Action, ApiError, Resource, require
from app.authz.principal import get_principal


def create_orchestrator():
    """Module seam over answer.factory.create_orchestrator (patchable).

    Import stays function-local so adapter clients are only needed
    when a search executes, never at route import or startup time.
    """
    from answer.factory import create_orchestrator as factory

    return factory()


router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["search"])

#: Metadata keys carrying an ISO date on indexed records (checked in order).
DATE_METADATA_KEYS = ("created_at", "updated_at", "date")


class SearchFilters(BaseModel):
    """Typed per-card filter set: type/source/date/collection/language."""

    type: str | None = None
    source: str | None = None
    date: str | None = None
    collection: str | None = None
    language: str | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    mode: RetrievalMode = RetrievalMode.HYBRID
    filters: SearchFilters = Field(default_factory=SearchFilters)


class SearchResultItem(BaseModel):
    chunk_id: str | None = None
    document_id: str | None = None
    content: str
    score: float
    method: str
    page: int | None = None
    section: str | None = None
    workspace_id: str | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    mode: str


def build_search_filter(*, workspace_id: str, filters: SearchFilters) -> SearchFilter:
    """Map typed request filters onto ``SearchFilter`` (pre-ranking, ACL-safe).

    The workspace scope is pinned by the caller; filters only narrow within it.
    ``type``/``source``/``collection`` become ``metadata`` equality predicates
    (honored pre-ranking by both stores); ``language`` uses the native language
    field. ``date`` is a post-retrieval predicate (see :func:`_matches_date`)
    and is intentionally NOT folded into ``metadata`` (records carry dates
    under several keys and formats, so exact-match pushdown would silently
    drop dated rows).
    """
    metadata: dict[str, Any] = {}
    if filters.type is not None:
        metadata["type"] = filters.type
    if filters.source is not None:
        metadata["source"] = filters.source
    if filters.collection is not None:
        metadata["collection"] = filters.collection
    return SearchFilter(
        workspace_ids=[workspace_id],
        language=filters.language,
        metadata=metadata,
    )


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _matches_date(metadata: dict[str, Any], predicate: str) -> bool:
    """Match a ``YYYY-MM-DD`` exact date or ``start..end`` range predicate."""
    predicate = predicate.strip()
    if ".." in predicate:
        start_raw, _, end_raw = predicate.partition("..")
        start = _parse_date(start_raw)
        end = _parse_date(end_raw)
        if start is None or end is None:
            return False
        for key in DATE_METADATA_KEYS:
            raw = metadata.get(key)
            if raw is None:
                continue
            parsed = _parse_date(str(raw))
            if parsed is not None and start <= parsed <= end:
                return True
        return False
    target = _parse_date(predicate)
    if target is None:
        return False
    for key in DATE_METADATA_KEYS:
        raw = metadata.get(key)
        if raw is None:
            continue
        if _parse_date(str(raw)) == target:
            return True
    return False


def _passes_post_filters(metadata: dict[str, Any], filters: SearchFilters) -> bool:
    """Narrowing-only post check for predicates not pushed into SearchFilter."""
    if filters.date is not None and not _matches_date(metadata, filters.date):
        return False
    return True


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Search workspace chunks (keyword / semantic / hybrid)",
)
async def search(
    workspace_id: uuid.UUID,
    body: SearchRequest,
    principal: Principal = Depends(get_principal),
) -> SearchResponse:
    try:
        require(
            principal,
            Action.DOCUMENT_READ,
            Resource(workspace_id=str(workspace_id)),
        )
    except ApiError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.code, "message": exc.message},
        ) from exc

    # Seam above keeps adapter imports lazy (see create_orchestrator).
    orchestrator = create_orchestrator()
    acl_scope = ACLScope(
        user_id=principal.user_id,
        workspace_ids=[str(workspace_id)],
    )
    query_filter = build_search_filter(workspace_id=str(workspace_id), filters=body.filters)
    results = await orchestrator.retrieve(
        query=body.query,
        acl_scope=acl_scope,
        mode=body.mode,
        extra_filter=query_filter,
    )

    narrowed = [item for item in results if _passes_post_filters(item.metadata, body.filters)]

    return SearchResponse(
        results=[
            SearchResultItem(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                content=item.content,
                score=item.score,
                method=item.method.value,
                page=item.page,
                section=item.section,
                workspace_id=item.workspace_id,
                scores=item.scores,
                metadata=item.metadata,
            )
            for item in narrowed
        ],
        mode=body.mode.value,
    )


class LegacySearchRequest(SearchRequest):
    workspace_id: uuid.UUID


legacy_router = APIRouter(prefix="/v1", tags=["search"])


@legacy_router.post(
    "/search",
    response_model=SearchResponse,
    summary="Search workspace chunks (legacy route alias)",
)
async def legacy_search(
    body: LegacySearchRequest,
    principal: Principal = Depends(get_principal),
) -> SearchResponse:
    return await search(workspace_id=body.workspace_id, body=body, principal=principal)

