"""ACL-aware hybrid retrieval orchestrator (rag-fork-solution TASK-M3-004).

This is our **custom core glue** (REUSE_STRATEGY: the one place custom code is
justified). Everything it orchestrates is reused behind `retrieval.interfaces`:

    ACLScope -> SearchFilter -> {dense, sparse, lexical} fan-out -> RRF
             -> rerank (optional) -> context selection -> per-item score trace

Guarantees (locked by `retrieval.contract_tests`):

- **ACL before ranking (R-004).** The scope becomes a `SearchFilter` that travels
  *with* every retriever call, and results are re-filtered defensively before
  fusion, so a store that ignores its filter still cannot leak.
- **Fail closed, never raise (boss ruling 2026-10-07).** An empty/unscoped
  `ACLScope` returns ``[]`` without calling any retriever. Store/rerank
  failures degrade (that leg contributes nothing) instead of raising.
- **Honest leg failure (T-V1-026).** A failing leg is never silently converted
  into an empty one. Every ``retrieve()`` call publishes a
  :class:`RetrievalResult` on :data:`retrieval_outcome_ctx` carrying a
  :class:`RetrievalStatus` (``ok`` / ``degraded`` / ``unavailable``), the
  canonical ``ReasonCode`` and a tracking ID. A **required** leg failing
  (``REQUIRED_LEGS``) makes the whole result ``unavailable`` and its items are
  **discarded** — a half-corpus answer presented as complete is the bug this
  exists to kill — while an **optional** leg (``sparse``, ``graph``, ``rerank``)
  degrades the result and keeps the items. An **empty corpus is not a
  failure**: every leg running cleanly and returning nothing is ``ok`` with
  ``items == []``.
- **Deterministic fusion.** ``score(d) = Σ 1/(k + rank)`` accumulated with
  ``math.fsum`` (order-independent), tie-broken by id ascending.
- **Trace rule (RAG debugger contract).** Every returned item always carries
  ``rrf`` and, when reranking ran, ``rerank``. The per-leg raw scores ``dense``,
  ``sparse`` and ``lexical`` are present exactly when that leg surfaced the
  item (``RetrievedItem.scores`` is ``dict[str, float]``, so an absent leg is an
  absent key, not a ``null``). ``TRACE_KEYS`` names the full set.
- **Sparse leg is an explicit seam.** `VectorStore` is dense-only, so sparse is
  reached through :class:`SparseRetriever` (any object with the lexical-shaped
  ``search``); when none is configured the leg is skipped (its trace key is then
  absent by the rule above). This is a documented seam, not a silent dead leg
  (G3).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Final, Protocol, runtime_checkable

from core.auth import ACLScope
from core.errors import ReasonCode, spec_for
from core.logging import new_request_id, request_id_ctx

from retrieval.embedding_identity import provider_identity

from .interfaces import (
    DEFAULT_CONTEXT_TOP_K,
    DEFAULT_DENSE_TOP_K,
    DEFAULT_FUSION_TOP_K,
    DEFAULT_LEXICAL_TOP_K,
    DEFAULT_RERANK_TOP_K,
    EmbeddingProvider,
    LexicalStore,
    Reranker,
    RetrievalMethod,
    RetrievalMode,
    RetrievalOrchestrator,
    RetrievedItem,
    SearchFilter,
    VectorStore,
)

logger = logging.getLogger("knowledge_os.retrieval.orchestrator")

#: RRF smoothing constant (canonical k=60; pinned by check_rrf_determinism).
RRF_K = 60

#: The six trace keys every returned item carries (debugger contract).
#: ``graph`` appears when the optional graph retriever leg surfaced the item
#: (see V2-GRAPH-PLAN G3/G4).
TRACE_KEYS = ("dense", "sparse", "lexical", "graph", "rrf", "rerank")

#: Visible disclosure when reranker is bypassed or absent (T-ARCH-03).
DENSE_ONLY_DISCLOSURE: Final[str] = (
    "local reranker deleted per user order, dense-only until LLM-rerank follow-up proves need"
)


# --- T-V1-026: honest per-leg failure statuses ------------------------------


class RetrievalStatus(StrEnum):
    """Terminal status of ONE retrieval (shared by search, ask and the UI).

    ``ok`` — every leg that ran succeeded; ``items`` may still be empty (an
    empty corpus is a fact, not a failure).
    ``degraded`` — the result stands, but an *optional* capability was
    unavailable on the way (reranker, graph, sparse seam).
    ``unavailable`` — a *required* leg for this mode failed, so ``items`` are
    discarded: the caller must report a failure, never a silent empty list.
    """

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class LegFailure:
    """One failed leg, safe to surface.

    ``error_type`` is the exception **class name only** — never ``str(exc)``,
    which can carry hostnames, SQL or prompt fragments. The reason code is the
    shared taxonomy member (``core.errors``), never a new literal.
    """

    leg: str
    error_type: str
    reason_code: ReasonCode

    @property
    def message(self) -> str:
        """Registry-backed safe user-facing message for this leg."""
        spec = spec_for(self.reason_code)
        return spec.message if spec is not None else ""


@dataclass(frozen=True)
class RetrievalResult:
    """Items plus the honest status of the run that produced them."""

    items: list[RetrievedItem]
    status: RetrievalStatus = RetrievalStatus.OK
    reason_code: ReasonCode | None = None
    trace_id: str = ""
    legs_failed: tuple[LegFailure, ...] = field(default_factory=tuple)
    rerank_disclosure: str = ""

    @property
    def reason(self) -> str:
        """Safe user-facing reason (registry message); empty when healthy."""
        if self.reason_code is None:
            return ""
        spec = spec_for(self.reason_code)
        return spec.message if spec is not None else ""

    @property
    def degraded(self) -> bool:
        return self.status is not RetrievalStatus.OK


#: Reason code emitted per leg. Codes come from the T-V1-025 shared taxonomy;
#: this module defines no code of its own (single-registry contract).
LEG_REASON_CODES: dict[str, ReasonCode] = {
    "embedding": ReasonCode.EMBED_UNAVAILABLE,
    "dense": ReasonCode.RETRIEVAL_UNAVAILABLE,
    "sparse": ReasonCode.DEGRADED_RESULT,
    "lexical": ReasonCode.RETRIEVAL_UNAVAILABLE,
    "graph": ReasonCode.GRAPH_UNAVAILABLE,
    "rerank": ReasonCode.RERANK_UNAVAILABLE,
}

#: Legs that MUST succeed for each mode, grounded in ``DEPENDENCY_MATRIX``
#: (``core.errors``): infinity embeddings and Qdrant/Postgres retrieval are
#: REQUIRED for ``search``/``ask``. ``graph`` is optional everywhere (matrix
#: OPTIONAL for search/ask) and ``sparse`` is the documented optional seam, so
#: neither can fail a request closed. ``graph`` mode therefore has no required
#: leg: it degrades, never fails.
REQUIRED_LEGS: dict[str, tuple[str, ...]] = {
    RetrievalMode.HYBRID.value: ("embedding", "dense", "lexical"),
    RetrievalMode.HYBRID_GRAPH.value: ("embedding", "dense", "lexical"),
    RetrievalMode.SEMANTIC.value: ("embedding", "dense"),
    RetrievalMode.KEYWORD.value: ("lexical",),
    RetrievalMode.GRAPH.value: (),
}


@dataclass
class _LegReport:
    """Per-call accumulator of leg failures (one per ``retrieve()`` call)."""

    failures: list[LegFailure] = field(default_factory=list)

    def record(self, leg: str, exc: BaseException, reason_code: ReasonCode) -> None:
        self.failures.append(
            LegFailure(leg=leg, error_type=type(exc).__name__, reason_code=reason_code)
        )
        logger.warning(
            "retrieval_leg_failed",
            extra={
                "event": "retrieval_leg_failed",
                "leg": leg,
                "error_type": type(exc).__name__,
                "reason_code": reason_code.value,
            },
        )

    def failed(self, leg: str) -> bool:
        return any(failure.leg == leg for failure in self.failures)


#: The outcome of the most recent ``retrieve()`` on this task. Context-local
#: (asyncio-safe) so a request, a worker job and a test each see only their own
#: run. Consumed with :func:`pop_retrieval_outcome`, which resets it, so a
#: stale outcome can never leak into a later call.
retrieval_outcome_ctx: ContextVar[RetrievalResult | None] = ContextVar(
    "retrieval_outcome", default=None
)


def pop_retrieval_outcome() -> RetrievalResult | None:
    """Read **and clear** the outcome published by the last ``retrieve()``."""
    outcome = retrieval_outcome_ctx.get()
    retrieval_outcome_ctx.set(None)
    return outcome


@runtime_checkable
class SparseRetriever(Protocol):
    """Explicit sparse-query seam (lexical-shaped search over a sparse index).

    `VectorStore` is dense-only, so a sparse leg (e.g. Qdrant's BM25 named
    vector) is supplied separately. Any object exposing this method works;
    ``None`` disables the leg.
    """

    async def search(
        self, *, query: str, top_k: int, query_filter: SearchFilter
    ) -> list[RetrievedItem]: ...


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], *, k: int = RRF_K
) -> dict[str, float]:
    """RRF: ``score(d) = Σ 1/(k + rank)`` over the rankings that contain ``d``.

    ``math.fsum`` makes the accumulation exactly rounded and independent of the
    order of the rankings, satisfying `check_rrf_determinism`.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    contributions: dict[str, list[float]] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            contributions.setdefault(doc_id, []).append(1.0 / (k + rank))
    return {doc_id: math.fsum(values) for doc_id, values in contributions.items()}


def sort_fused(scores: dict[str, float]) -> list[str]:
    """Deterministic order: score desc, then id asc (no dict-order luck)."""
    return sorted(scores, key=lambda doc_id: (-scores[doc_id], doc_id))


def _item_key(item: RetrievedItem) -> str:
    return item.chunk_id or item.content


class CloudEmbeddingEgressRefused(RuntimeError):
    """Fail-closed: a sensitive/local-only workspace cannot use a cloud embedder.

    Raised conceptually (without network contact) when a sensitive workspace
    asks for semantic or hybrid retrieval. Keyword-only retrieval keeps working.
    """

    def __init__(self, message: str = "embedding egress refused for local-only workspace"):
        super().__init__(message)



class RefusingEmbeddingProvider(EmbeddingProvider):
    """Embedding provider that refuses before any network contact (T-A01)."""

    model_name: ClassVar[str] = "local-only-refused"
    dimension: ClassVar[int] = 0
    revision: ClassVar[str] = "local-only-refused"

    async def embed_documents(self, texts):
        raise CloudEmbeddingEgressRefused(
            "embedding egress refused for local-only workspace"
        )

    async def embed_query(self, text):
        raise CloudEmbeddingEgressRefused(
            "embedding egress refused for local-only workspace"
        )


class HybridRetrievalOrchestrator(RetrievalOrchestrator):
    """Product hybrid orchestrator over reused store/embedding/reranker adapters."""

    def __init__(
        self,
        *,
        dense: VectorStore,
        embedding: EmbeddingProvider,
        lexical: LexicalStore,
        collection: str,
        sparse: SparseRetriever | None = None,
        reranker: Reranker | None = None,
        rerank_enabled: bool = True,
        rrf_k: int = RRF_K,
        graph: Any | None = None,
        sensitive: bool = False,
        local_only: bool = False,
        workspace_sensitive: bool = False,
    ) -> None:
        self.dense = dense
        self.embedding = embedding
        self.lexical = lexical
        self.collection = collection
        self.sparse = sparse
        self.reranker = reranker
        self.rerank_enabled = rerank_enabled
        self.rrf_k = rrf_k
        self.graph = graph
        self.sensitive = bool(sensitive or local_only or workspace_sensitive)

    # --- fan-out ----------------------------------------------------------

    async def _dense_leg(
        self,
        query: str,
        top_k: int,
        query_filter: SearchFilter,
        *,
        report: _LegReport | None = None,
    ) -> list[RetrievedItem]:
        """Embed the query, then search the dense store — two attributable legs.

        The split is what lets a failure name the dependency that broke:
        ``embed_query`` blowing up is an embeddings outage (``EMBED_UNAVAILABLE``),
        the store blowing up is a retrieval outage (``RETRIEVAL_UNAVAILABLE``).
        With no ``report`` the leg raises (legacy caller contract).
        """
        try:
            vector = await self.embedding.embed_query(query)
        except Exception as exc:
            if report is None:
                raise
            report.record("embedding", exc, LEG_REASON_CODES["embedding"])
            return []
        # T-A05: production dense search derives identity from the provider
        # plus the observed vector dimension and threads it through the store
        # boundary. observed dimension means we catch provider/dimension lies.
        query_identity = provider_identity(self.embedding, len(vector))
        try:
            return await self.dense.search(
                collection=self.collection,
                vector=vector,
                top_k=top_k,
                query_filter=query_filter,
                query_identity=query_identity,
            )
        except Exception as exc:
            if report is None:
                raise
            report.record("dense", exc, LEG_REASON_CODES["dense"])
            return []

    async def _sparse_leg(
        self,
        query: str,
        top_k: int,
        query_filter: SearchFilter,
        *,
        report: _LegReport | None = None,
    ) -> list[RetrievedItem]:
        if self.sparse is None:
            return []
        if report is None:
            return await self.sparse.search(
                query=query, top_k=top_k, query_filter=query_filter
            )
        try:
            return await self.sparse.search(
                query=query, top_k=top_k, query_filter=query_filter
            )
        except Exception as exc:
            report.record("sparse", exc, LEG_REASON_CODES["sparse"])
            return []

    async def _lexical_leg(
        self, query: str, top_k: int, query_filter: SearchFilter, *, report: _LegReport
    ) -> list[RetrievedItem]:
        try:
            return await self.lexical.search(
                query=query, top_k=top_k, query_filter=query_filter
            )
        except Exception as exc:
            report.record("lexical", exc, LEG_REASON_CODES["lexical"])
            return []

    async def _fan_out(
        self,
        query: str,
        query_filter: SearchFilter,
        *,
        dense_top_k: int,
        sparse_top_k: int,
        lexical_top_k: int,
        report: _LegReport,
    ) -> tuple[list[RetrievedItem], list[RetrievedItem], list[RetrievedItem]]:
        """Run the three legs concurrently; a failing leg records and yields ``[]``.

        The failure is never silent: it lands on ``report`` (T-V1-026), which
        decides whether it degrades the result or fails it closed.
        """
        dense_items, sparse_items, lexical_items = await asyncio.gather(
            self._dense_leg(query, dense_top_k, query_filter, report=report),
            self._sparse_leg(query, sparse_top_k, query_filter, report=report),
            self._lexical_leg(query, lexical_top_k, query_filter, report=report),
        )
        return dense_items, sparse_items, lexical_items

    async def _graph_leg(
        self, query: str, workspace_ids: Sequence[str], *, report: _LegReport
    ) -> list[RetrievedItem]:
        if self.graph is None:
            return []
        items: list[RetrievedItem] = []
        for ws_id in workspace_ids:
            try:
                local_res = await self.graph.local_search(workspace_id=ws_id, query=query)
                items.extend(self.graph.to_retrieved_items(local_res, workspace_id=ws_id))
                global_res = await self.graph.global_search(workspace_id=ws_id, query=query)
                items.extend(self.graph.to_retrieved_items(global_res, workspace_id=ws_id))
            except Exception as exc:
                report.record("graph", exc, LEG_REASON_CODES["graph"])
        return items

    # --- ACL --------------------------------------------------------------

    @staticmethod
    def _in_scope(item: RetrievedItem, allowed: set[str], include_public: bool) -> bool:
        if item.workspace_id is None:
            return False
        return include_public or item.workspace_id in allowed

    # --- pipeline ---------------------------------------------------------

    async def retrieve_result(
        self,
        *,
        query: str,
        acl_scope: ACLScope,
        mode: RetrievalMode = RetrievalMode.HYBRID,
        extra_filter: SearchFilter | None = None,
        dense_top_k: int = DEFAULT_DENSE_TOP_K,
        lexical_top_k: int = DEFAULT_LEXICAL_TOP_K,
        fusion_top_k: int = DEFAULT_FUSION_TOP_K,
        rerank_top_k: int = DEFAULT_RERANK_TOP_K,
        context_top_k: int = DEFAULT_CONTEXT_TOP_K,
        sparse_top_k: int = DEFAULT_LEXICAL_TOP_K,
    ) -> RetrievalResult:
        """Run the pipeline and report the honest outcome (T-V1-026).

        Same retrieval as :meth:`retrieve`, plus the status, the canonical
        reason code, the failed legs and a tracking ID. This is the method to
        call when "no results" must be distinguishable from "search is down".
        """
        trace_id = request_id_ctx.get() or new_request_id()
        report = _LegReport()

        # G0: empty/unscoped scope fails closed, before any retriever call.
        # Not a failure: nothing was attempted.
        if acl_scope.is_empty:
            return RetrievalResult(items=[], trace_id=trace_id)
        # Micro #12: a blank/whitespace query can match nothing; return early
        # (no retriever/embedding calls). Matches the PG FTS store guard.
        if not query.strip():
            return RetrievalResult(items=[], trace_id=trace_id)

        # T-A01: sensitive/local-only workspaces must not egress query
        # embeddings. Refuse semantic/hybrid retrieval BEFORE any embedding
        # request (keyword/graph paths keep working). Typed + logged; empty
        # result, never a silent fallback.
        if self.sensitive and mode in (
            RetrievalMode.SEMANTIC,
            RetrievalMode.HYBRID,
            RetrievalMode.HYBRID_GRAPH,
        ):
            logger.warning(
                "retrieval_egress_refused",
                extra={
                    "event": "retrieval_egress_refused",
                    "leg": "embedding",
                    "reason": "local-only workspace refused cloud embedding transport",
                    "mode": mode.value,
                    "trace_id": trace_id,
                },
            )
            failure = LegFailure(
                leg="embedding",
                error_type="CloudEmbeddingEgressRefused",
                reason_code=ReasonCode.EMBED_UNAVAILABLE,
            )
            return RetrievalResult(
                items=[],
                status=RetrievalStatus.UNAVAILABLE,
                reason_code=ReasonCode.EMBED_UNAVAILABLE,
                trace_id=trace_id,
                legs_failed=(failure,),
            )
        allowed = set(acl_scope.workspace_ids)
        query_filter = SearchFilter(workspace_ids=list(acl_scope.workspace_ids))
        if extra_filter is not None:
            if extra_filter.language:
                query_filter.language = extra_filter.language
            for _k, _v in (extra_filter.metadata or {}).items():
                query_filter.metadata[_k] = _v
            if extra_filter.document_ids:
                query_filter.document_ids = list(extra_filter.document_ids)
        # Mode selector: keyword = lexical-only (M3-003 FTS), semantic = dense-only
        # (M3-001 Qdrant), hybrid = default (existing RRF fusion, unchanged),
        # graph = graph-only, hybrid_graph = dense + lexical + graph fusion.
        dense_items: list[RetrievedItem] = []
        sparse_items: list[RetrievedItem] = []
        lexical_items: list[RetrievedItem] = []
        graph_items: list[RetrievedItem] = []
        if mode in (RetrievalMode.HYBRID, RetrievalMode.HYBRID_GRAPH):
            dense_items, sparse_items, lexical_items = await self._fan_out(
                query,
                query_filter,
                dense_top_k=dense_top_k,
                sparse_top_k=sparse_top_k,
                lexical_top_k=lexical_top_k,
                report=report,
            )
            if mode == RetrievalMode.HYBRID_GRAPH:
                graph_items = await self._graph_leg(
                    query, acl_scope.workspace_ids, report=report
                )
        elif mode == RetrievalMode.GRAPH:
            graph_items = await self._graph_leg(query, acl_scope.workspace_ids, report=report)
        elif mode == RetrievalMode.SEMANTIC:
            dense_items = await self._dense_leg(
                query, dense_top_k, query_filter, report=report
            )
        elif mode == RetrievalMode.KEYWORD:
            lexical_items = await self._lexical_leg(
                query, lexical_top_k, query_filter, report=report
            )

        # Required-leg verdict: a REQUIRED leg for this mode that failed fails
        # the whole result closed, with its items discarded (never a silent
        # empty list, never a half-corpus answer presented as complete).
        required = REQUIRED_LEGS.get(mode.value, ())
        blocking = [failure for failure in report.failures if failure.leg in required]
        if blocking:
            primary = blocking[0]
            return RetrievalResult(
                items=[],
                status=RetrievalStatus.UNAVAILABLE,
                reason_code=primary.reason_code,
                trace_id=trace_id,
                legs_failed=tuple(report.failures),
            )

        # Defense in depth: never rank/fuse an out-of-scope row even if a store
        # ignored its filter (R-004). Provenance-poor rows are dropped too.
        public = acl_scope.include_public
        legs = {
            "dense": [i for i in dense_items if self._in_scope(i, allowed, public)],
            "sparse": [i for i in sparse_items if self._in_scope(i, allowed, public)],
            "lexical": [i for i in lexical_items if self._in_scope(i, allowed, public)],
            "graph": [i for i in graph_items if self._in_scope(i, allowed, public)],
        }

        # Raw per-leg scores for the trace (item key -> {leg: score}). A leg that
        # did not surface an item simply has no key (documented trace rule).
        trace: dict[str, dict[str, float]] = {}
        by_key: dict[str, RetrievedItem] = {}
        rankings: list[list[str]] = []
        for leg_name in ("dense", "sparse", "lexical", "graph"):
            items = legs[leg_name]
            if not items:
                continue
            rankings.append([_item_key(item) for item in items])
            for item in items:
                key = _item_key(item)
                trace.setdefault(key, {}).setdefault(leg_name, item.score)
                by_key.setdefault(key, item)

        fused = reciprocal_rank_fusion(rankings, k=self.rrf_k)
        ranked_keys = sort_fused(fused)[:fusion_top_k]

        fused_items: list[RetrievedItem] = []
        for key in ranked_keys:
            source = by_key[key]
            item = source.model_copy(deep=True)
            item.method = RetrievalMethod.FUSED
            item.score = fused[key]
            scores = dict(trace.get(key, {}))
            scores["rrf"] = fused[key]
            item.scores = scores
            fused_items.append(item)

        # Rerank (optional). A reranker outage degrades to RRF order, loudly.
        # T-ARCH-03: When reranker is deleted/disabled/bypassed, run dense-only with disclosure.
        final_items = fused_items
        rerank_used = False
        rerank_disclosure = ""
        if self.rerank_enabled and self.reranker is not None and fused_items:
            try:
                reranked = await self.reranker.rerank(
                    query=query, documents=fused_items, top_k=rerank_top_k
                )
            except Exception as exc:  # degrade, never 500 silently
                report.record("rerank", exc, LEG_REASON_CODES["rerank"])
                logger.warning(
                    "rerank_fallback",
                    extra={
                        "event": "rerank_fallback",
                        "error_type": type(exc).__name__,
                        "trace_id": trace_id,
                    },
                )
                rerank_disclosure = DENSE_ONLY_DISCLOSURE
            else:
                if reranked:
                    prepared: list[RetrievedItem] = []
                    for item in reranked:
                        # Micro #11: defensive re-filter AFTER rerank. A reranker
                        # must return a subset of its input; a non-conforming one
                        # cannot inject an out-of-scope or provenance-poor row.
                        if not self._in_scope(item, allowed, public):
                            logger.warning(
                                "rerank_out_of_scope_dropped",
                                extra={
                                    "event": "rerank_out_of_scope_dropped",
                                    "workspace_id": item.workspace_id,
                                },
                            )
                            continue
                        copy = item.model_copy(deep=True)
                        rerank_score = float(copy.score)
                        scores = dict(copy.scores)
                        scores["rerank"] = rerank_score
                        copy.scores = scores
                        copy.method = RetrievalMethod.RERANKED
                        copy.score = rerank_score
                        prepared.append(copy)
                    final_items = prepared
                    rerank_used = True
                else:
                    rerank_disclosure = DENSE_ONLY_DISCLOSURE
        else:
            rerank_disclosure = DENSE_ONLY_DISCLOSURE
            logger.info(
                "retrieval_dense_only",
                extra={
                    "event": "retrieval_dense_only",
                    "reason": DENSE_ONLY_DISCLOSURE,
                    "trace_id": trace_id,
                },
            )

        selected = final_items[:context_top_k]
        for item in selected:
            # Guarantee the trace keys that must always be present.
            scores = dict(item.scores)
            scores.setdefault("rrf", float(item.score))
            if rerank_used:
                scores["rerank"] = float(item.score)
            item.scores = scores
            if rerank_disclosure:
                item.metadata["rerank_disclosure"] = rerank_disclosure

        # Optional-leg verdict: the items stand, the degradation is disclosed.
        # The top-level code is the STATUS-level one (``RERANK_UNAVAILABLE`` or
        # the generic ``DEGRADED_RESULT``) so it always satisfies
        # ``ANSWER_STATUS_REASON_CODES[degraded]``; the per-leg codes
        # (``GRAPH_UNAVAILABLE``, …) stay visible on ``legs_failed``.
        status = RetrievalStatus.OK
        reason_code: ReasonCode | None = None
        if report.failures:
            status = RetrievalStatus.DEGRADED
            reason_code = (
                LEG_REASON_CODES["rerank"]
                if report.failed("rerank")
                else ReasonCode.DEGRADED_RESULT
            )
        return RetrievalResult(
            items=selected,
            status=status,
            reason_code=reason_code,
            trace_id=trace_id,
            legs_failed=tuple(report.failures),
            rerank_disclosure=rerank_disclosure,
        )

    async def retrieve(
        self,
        *,
        query: str,
        acl_scope: ACLScope,
        mode: RetrievalMode = RetrievalMode.HYBRID,
        extra_filter: SearchFilter | None = None,
        dense_top_k: int = DEFAULT_DENSE_TOP_K,
        lexical_top_k: int = DEFAULT_LEXICAL_TOP_K,
        fusion_top_k: int = DEFAULT_FUSION_TOP_K,
        rerank_top_k: int = DEFAULT_RERANK_TOP_K,
        context_top_k: int = DEFAULT_CONTEXT_TOP_K,
        sparse_top_k: int = DEFAULT_LEXICAL_TOP_K,
    ) -> list[RetrievedItem]:
        """Items only — the legacy contract, unchanged for existing callers.

        T-V1-026: the honest outcome is **not** lost on this path. It is
        published to :data:`retrieval_outcome_ctx`, which the answer engine and
        the search route read with :func:`pop_retrieval_outcome`. Callers that
        need the status directly should use :meth:`retrieve_result`.
        """
        result = await self.retrieve_result(
            query=query,
            acl_scope=acl_scope,
            mode=mode,
            extra_filter=extra_filter,
            dense_top_k=dense_top_k,
            lexical_top_k=lexical_top_k,
            fusion_top_k=fusion_top_k,
            rerank_top_k=rerank_top_k,
            context_top_k=context_top_k,
            sparse_top_k=sparse_top_k,
        )
        retrieval_outcome_ctx.set(result)
        return result.items
