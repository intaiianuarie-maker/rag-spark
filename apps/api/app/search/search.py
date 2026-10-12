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

T-V1-026 (honest failure statuses): the response now carries ``status`` /
``reason_code`` / ``reason`` / ``trace_id``. An **empty corpus is still a 200
with ``results: []`` and ``status: "ok"``**; a **required** retrieval leg
failing (embeddings, Qdrant, Postgres FTS) is a **503** with the registered
safe reason and the tracking ID, never an empty list. An optional leg (rerank)
degrades to ``status: "degraded"`` with the results intact.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from core.auth import ACLScope, Principal
from core.errors import ReasonCode, error_detail, spec_for
from core.logging import request_id_ctx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from retrieval.interfaces import RetrievalMode, SearchFilter
from retrieval.orchestrator import RetrievalResult, RetrievalStatus, pop_retrieval_outcome

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
    #: T-V1-026: honest per-run status. ``ok`` (results may still be empty —
    #: an empty corpus is a fact, not a failure), ``degraded`` (an optional
    #: capability such as the reranker was unavailable; results stand) or
    #: ``unavailable`` (never here: a required leg failing returns 503 instead,
    #: so "no results" and "search is down" cannot be confused). ``trace_id``
    #: is the tracking ID for support; it is present on every response.
    status: str = RetrievalStatus.OK.value
    reason_code: str | None = None
    reason: str | None = None
    trace_id: str | None = None


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


def _retrieval_outcome() -> RetrievalResult:
    """Outcome of the ``retrieve()`` call this handler just made (T-V1-026).

    ``pop_retrieval_outcome`` reads the context-local result published by
    :class:`retrieval.orchestrator.HybridRetrievalOrchestrator` and clears it,
    so a stale outcome can never be served to a later request. Test doubles and
    orchestrators that predate this ticket publish nothing and get the healthy
    default (``ok``), which keeps every existing fake working unchanged.
    """
    return pop_retrieval_outcome() or RetrievalResult(items=[])


def _unavailable_status(reason_code: ReasonCode | None) -> int:
    """Registered HTTP status for the failure, defaulting to 503 (retryable)."""
    spec = spec_for(reason_code) if reason_code is not None else None
    return spec.http_status if spec is not None and spec.http_status else 503


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
    # T-V1-026: read the honest outcome the orchestrator published for THIS
    # call (context-local, so concurrent requests never read each other's).
    # The items themselves always come from the call's return value, so an
    # orchestrator that predates this ticket (or a test double) behaves
    # exactly as before and simply reports a healthy ``ok``.
    outcome = _retrieval_outcome()

    if outcome.status is RetrievalStatus.UNAVAILABLE:
        # A required leg failed: fail closed with the registered safe reason +
        # the tracking ID. Never an empty 200 that reads as "no results".
        raise HTTPException(
            status_code=_unavailable_status(outcome.reason_code),
            detail={
                **error_detail(outcome.reason_code or ReasonCode.RETRIEVAL_UNAVAILABLE),
                "trace_id": outcome.trace_id,
            },
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
        status=outcome.status.value,
        reason_code=outcome.reason_code.value if outcome.reason_code else None,
        reason=outcome.reason or None,
        trace_id=outcome.trace_id or request_id_ctx.get(),
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

