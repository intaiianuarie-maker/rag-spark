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
from typing import Any, Protocol, runtime_checkable

from core.auth import ACLScope

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

#: The five trace keys every returned item carries (debugger contract).
TRACE_KEYS = ("dense", "sparse", "lexical", "rrf", "rerank")


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

    # --- fan-out ----------------------------------------------------------

    async def _dense_leg(
        self, query: str, top_k: int, query_filter: SearchFilter
    ) -> list[RetrievedItem]:
        vector = await self.embedding.embed_query(query)
        return await self.dense.search(
            collection=self.collection,
            vector=vector,
            top_k=top_k,
            query_filter=query_filter,
        )

    async def _sparse_leg(
        self, query: str, top_k: int, query_filter: SearchFilter
    ) -> list[RetrievedItem]:
        if self.sparse is None:
            return []
        return await self.sparse.search(
            query=query, top_k=top_k, query_filter=query_filter
        )

    async def _fan_out(
        self,
        query: str,
        query_filter: SearchFilter,
        *,
        dense_top_k: int,
        sparse_top_k: int,
        lexical_top_k: int,
    ) -> tuple[list[RetrievedItem], list[RetrievedItem], list[RetrievedItem]]:
        """Run the three legs concurrently; a failing leg degrades to ``[]``."""
        legs = await asyncio.gather(
            self._dense_leg(query, dense_top_k, query_filter),
            self._sparse_leg(query, sparse_top_k, query_filter),
            self.lexical.search(query=query, top_k=lexical_top_k, query_filter=query_filter),
            return_exceptions=True,
        )
        names = ("dense", "sparse", "lexical")
        out: list[list[RetrievedItem]] = []
        for name, result in zip(names, legs, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    f"retrieval_leg_failed: leg={name} error={result}",
                    extra={"event": "retrieval_leg_failed", "leg": name, "error": str(result)},
                )
                out.append([])
            else:
                out.append(list(result))
        return out[0], out[1], out[2]

    async def _graph_leg(
        self, query: str, workspace_ids: Sequence[str]
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
                logger.warning(
                    "retrieval_leg_failed",
                    extra={"event": "retrieval_leg_failed", "leg": "graph", "error": str(exc)},
                )
        return items

    # --- ACL --------------------------------------------------------------

    @staticmethod
    def _in_scope(item: RetrievedItem, allowed: set[str], include_public: bool) -> bool:
        if item.workspace_id is None:
            return False
        return include_public or item.workspace_id in allowed

    # --- pipeline ---------------------------------------------------------

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
        # G0: empty/unscoped scope fails closed, before any retriever call.
        if acl_scope.is_empty:
            return []
        # Micro #12: a blank/whitespace query can match nothing; return early
        # (no retriever/embedding calls). Matches the PG FTS store guard.
        if not query.strip():
            return []

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
        if mode == RetrievalMode.HYBRID:
            dense_items, sparse_items, lexical_items = await self._fan_out(
                query,
                query_filter,
                dense_top_k=dense_top_k,
                sparse_top_k=sparse_top_k,
                lexical_top_k=lexical_top_k,
            )
        elif mode == RetrievalMode.HYBRID_GRAPH:
            dense_items, sparse_items, lexical_items = await self._fan_out(
                query,
                query_filter,
                dense_top_k=dense_top_k,
                sparse_top_k=sparse_top_k,
                lexical_top_k=lexical_top_k,
            )
            graph_items = await self._graph_leg(query, acl_scope.workspace_ids)
        elif mode == RetrievalMode.GRAPH:
            graph_items = await self._graph_leg(query, acl_scope.workspace_ids)
        elif mode == RetrievalMode.SEMANTIC:
            # Degrade like the hybrid fan-out: a dense/embedding failure
            # yields [] for that mode instead of a 500 (fail-closed rule).
            try:
                dense_items = await self._dense_leg(query, dense_top_k, query_filter)
            except Exception as exc:
                logger.warning(
                    "retrieval_leg_failed",
                    extra={"event": "retrieval_leg_failed", "leg": "dense", "error": str(exc)},
                )
                dense_items = []
        elif mode == RetrievalMode.KEYWORD:
            try:
                lexical_items = await self.lexical.search(
                    query=query, top_k=lexical_top_k, query_filter=query_filter
                )
            except Exception as exc:
                logger.warning(
                    "retrieval_leg_failed",
                    extra={"event": "retrieval_leg_failed", "leg": "lexical", "error": str(exc)},
                )
                lexical_items = []

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
        final_items = fused_items
        rerank_used = False
        if self.rerank_enabled and self.reranker is not None and fused_items:
            try:
                reranked = await self.reranker.rerank(
                    query=query, documents=fused_items, top_k=rerank_top_k
                )
            except Exception as exc:  # degrade, never 500 silently
                logger.warning(
                    "rerank_fallback",
                    extra={"event": "rerank_fallback", "error": str(exc)},
                )
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

        selected = final_items[:context_top_k]
        for item in selected:
            # Guarantee the trace keys that must always be present.
            scores = dict(item.scores)
            scores.setdefault("rrf", float(item.score))
            if rerank_used:
                scores["rerank"] = float(item.score)
            item.scores = scores
        return selected
