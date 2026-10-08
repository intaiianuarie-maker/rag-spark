"""Unit tests for the product hybrid orchestrator (TASK-M3-004).

Covers the ticket's acceptance directly: deterministic RRF math, ACL enforced
before ranking (incl. a poisoned store), context selection, and the per-item
score trace; plus the documented degradation rules (missing store leg, reranker
down) and the fail-closed empty-scope ruling.
"""

from __future__ import annotations

import math

import pytest
from core.auth import ACLScope
from harness_fakes import (
    FakeEmbeddingProvider,
    FakeLexicalStore,
    FakeReranker,
    FakeVectorStore,
    make_item,
    make_record,
)
from retrieval.adapters.infinity_rerank import RerankerError
from retrieval.contract_tests import check_rrf_determinism
from retrieval.interfaces import Reranker, RetrievalMethod, SearchFilter
from retrieval.orchestrator import (
    HybridRetrievalOrchestrator,
    reciprocal_rank_fusion,
    sort_fused,
)

pytestmark = pytest.mark.unit

COLLECTION = "orch"
DIMENSION = 8


# --- RRF math (C2/C3) -------------------------------------------------------


def test_rrf_matches_the_definition() -> None:
    k = 60
    rankings = [["d1", "d2", "d3"], ["d3", "d1"], ["d2"]]
    fused = reciprocal_rank_fusion(rankings, k=k)
    assert fused["d1"] == pytest.approx(1 / (k + 1) + 1 / (k + 2), abs=1e-12)
    assert fused["d2"] == pytest.approx(1 / (k + 2) + 1 / (k + 1), abs=1e-12)
    assert fused["d3"] == pytest.approx(1 / (k + 3) + 1 / (k + 1), abs=1e-12)


def test_rrf_missing_leg_and_empty() -> None:
    k = 60
    fused = reciprocal_rank_fusion([["only"], []], k=k)
    assert fused == {"only": pytest.approx(1 / (k + 1), abs=1e-12)}
    assert reciprocal_rank_fusion([], k=k) == {}


def test_rrf_tie_breaks_by_id_ascending() -> None:
    fused = reciprocal_rank_fusion([["b"], ["a"]], k=60)
    assert fused["a"] == fused["b"]  # symmetric inputs -> equal scores
    assert sort_fused(fused) == ["a", "b"]  # deterministic tie-break


def test_rrf_is_order_independent() -> None:
    check_rrf_determinism(reciprocal_rank_fusion)


def test_rrf_rejects_bad_k() -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([["a"]], k=0)


# --- helpers ----------------------------------------------------------------


def _records() -> list:
    def vector(*head: float) -> list[float]:
        return [*head, *([0.0] * (DIMENSION - len(head)))]

    return [
        make_record(
            "a-1",
            "ws-a",
            "hybrid retrieval evidence about permissions",
            vector=vector(1.0),
            page=1,
            section="Intro",
        ),
        make_record(
            "a-2",
            "ws-a",
            "hybrid retrieval ranks evidence before context selection",
            vector=vector(0.8, 0.2),
            page=2,
            section="Method",
        ),
        make_record(
            "b-1",
            "ws-b",
            "bravo secret phrase hybrid retrieval",
            vector=vector(1.0),
            document_id="doc-b",
            page=1,
            section="Secret",
        ),
    ]


async def _orchestrator(
    *, sparse: bool = False, reranker: Reranker | None = None, rerank_enabled: bool = True
) -> tuple[HybridRetrievalOrchestrator, FakeVectorStore, FakeLexicalStore]:
    dense = FakeVectorStore(dimension=DIMENSION)
    lexical = FakeLexicalStore()
    await dense.ensure_collection(collection=COLLECTION, dimension=DIMENSION)
    records = _records()
    await dense.upsert(collection=COLLECTION, records=records)
    await lexical.index(records=records)
    sparse_store = None
    if sparse:
        sparse_store = FakeLexicalStore()
        await sparse_store.index(records=records)
    orchestrator = HybridRetrievalOrchestrator(
        dense=dense,
        embedding=FakeEmbeddingProvider(),
        lexical=lexical,
        collection=COLLECTION,
        sparse=sparse_store,
        reranker=reranker,
        rerank_enabled=rerank_enabled,
    )
    return orchestrator, dense, lexical


# --- ACL (C5/C6/C8) ---------------------------------------------------------


async def test_empty_scope_returns_empty_and_calls_no_retriever() -> None:
    orchestrator, dense, lexical = await _orchestrator()
    before_dense, before_lexical = len(dense.search_calls), len(lexical.search_calls)
    result = await orchestrator.retrieve(query="hybrid retrieval", acl_scope=ACLScope(user_id="u1"))
    assert result == []
    assert len(dense.search_calls) == before_dense
    assert len(lexical.search_calls) == before_lexical


async def test_results_are_workspace_scoped() -> None:
    orchestrator, _, _ = await _orchestrator()
    items = await orchestrator.retrieve(
        query="hybrid retrieval", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert items
    assert {item.workspace_id for item in items} == {"ws-a"}


class _PoisonedVectorStore(FakeVectorStore):
    """Returns an out-of-scope row with a perfect score, ignoring its filter."""

    async def search(self, *, collection, vector, top_k, query_filter):  # type: ignore[override]
        self.search_calls.append(query_filter)
        return [
            make_item(
                "bravo secret phrase",
                1.0,
                workspace_id="ws-b",
                document_id="doc-b",
                chunk_id="b-1",
            ),
            make_item(
                "alpha evidence",
                0.1,
                workspace_id="ws-a",
                document_id="doc-a",
                chunk_id="a-1",
            ),
        ]


async def test_poisoned_store_cannot_leak() -> None:
    dense = _PoisonedVectorStore(dimension=DIMENSION)
    lexical = FakeLexicalStore()
    orchestrator = HybridRetrievalOrchestrator(
        dense=dense,
        embedding=FakeEmbeddingProvider(),
        lexical=lexical,
        collection=COLLECTION,
    )
    items = await orchestrator.retrieve(
        query="bravo secret phrase",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert items, "in-scope row must survive"
    assert all(item.workspace_id == "ws-a" for item in items)


# --- trace + fusion (C9/C13) ------------------------------------------------


async def test_trace_and_provenance() -> None:
    orchestrator, _, _ = await _orchestrator()
    items = await orchestrator.retrieve(
        query="hybrid retrieval evidence",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
        fusion_top_k=5,
    )
    assert items
    for item in items:
        assert "rrf" in item.scores, "rrf trace always present"
        # dense/lexical present only if that leg surfaced the item (documented rule)
        assert set(item.scores) <= {"dense", "sparse", "lexical", "rrf", "rerank"}
        assert item.method == RetrievalMethod.FUSED
        assert item.document_id and item.chunk_id
        assert item.content
        assert math.isfinite(item.score)
    # both legs contributed on this fixture
    assert any("dense" in item.scores for item in items)
    assert any("lexical" in item.scores for item in items)


async def test_sparse_leg_is_used_when_configured() -> None:
    orchestrator, _, _ = await _orchestrator(sparse=True)
    items = await orchestrator.retrieve(
        query="hybrid retrieval evidence",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
    )
    assert any("sparse" in item.scores for item in items)


async def test_context_selection_bounds_output() -> None:
    orchestrator, _, _ = await _orchestrator()
    items = await orchestrator.retrieve(
        query="hybrid retrieval evidence",
        acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
        context_top_k=1,
    )
    assert len(items) == 1


async def test_deterministic_across_calls() -> None:
    orchestrator, _, _ = await _orchestrator(reranker=FakeReranker())
    scope = ACLScope(user_id="u1", workspace_ids=["ws-a"])
    first = await orchestrator.retrieve(query="hybrid retrieval", acl_scope=scope)
    second = await orchestrator.retrieve(query="hybrid retrieval", acl_scope=scope)
    assert [item.model_dump() for item in first] == [item.model_dump() for item in second]


# --- rerank (C10/C12) -------------------------------------------------------


class _InvertingReranker(Reranker):
    """Scores by a fixed map so the result inverts the RRF order."""

    model_name = "inverting"

    async def rerank(self, *, query, documents, top_k=None):
        order = {item.chunk_id: float(index) for index, item in enumerate(documents)}
        ranked = []
        for item in documents:
            copy = item.model_copy(deep=True)
            copy.score = float(order[item.chunk_id])
            copy.method = RetrievalMethod.RERANKED
            copy.scores = {**copy.scores, "rerank": copy.score}
            ranked.append(copy)
        ranked.sort(key=lambda item: (-item.score, item.content))
        return ranked if top_k is None else ranked[:top_k]


async def test_rerank_reorders_and_sets_trace() -> None:
    orchestrator, _, _ = await _orchestrator(reranker=_InvertingReranker())
    items = await orchestrator.retrieve(
        query="hybrid retrieval", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert items
    assert items[0].method == RetrievalMethod.RERANKED
    assert all("rerank" in item.scores for item in items)
    assert [item.score for item in items] == sorted((item.score for item in items), reverse=True)


async def test_rerank_disabled_keeps_rrf_order() -> None:
    orchestrator, _, _ = await _orchestrator(reranker=_InvertingReranker(), rerank_enabled=False)
    items = await orchestrator.retrieve(
        query="hybrid retrieval", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert items
    assert all(item.method == RetrievalMethod.FUSED for item in items)
    assert all("rerank" not in item.scores for item in items)


class _BrokenReranker(Reranker):
    model_name = "broken"

    async def rerank(self, *, query, documents, top_k=None):
        raise RerankerError("infinity /rerank down")


async def test_rerank_down_degrades_to_rrf(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _enable_orchestrator_logger(monkeypatch)
    orchestrator, _, _ = await _orchestrator(reranker=_BrokenReranker())
    with caplog.at_level("WARNING"):
        items = await orchestrator.retrieve(
            query="hybrid retrieval", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
        )
    assert items, "must still return RRF results"
    assert all(item.method == RetrievalMethod.FUSED for item in items)
    assert "rerank_fallback" in caplog.text


# --- store degradation (C4) -------------------------------------------------


def _enable_orchestrator_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-enable the orchestrator logger for the duration of a caplog assertion.

    Alembic's ``env.py`` calls ``logging.config.fileConfig`` with the default
    ``disable_existing_loggers=True`` on every ``run_migrations()`` (any
    integration test), which sets ``disabled=True`` on our logger for the rest
    of the process; ``logger.warning`` then becomes a no-op and ``caplog.text``
    is empty even though the code is correct (M2-002 F1 class). The root fix in
    ``apps/api/alembic/env.py`` is out of scope, so the test re-enables its own
    logger under monkeypatch. Do not remove: it is what keeps these assertions
    true in a full-suite run.
    """
    import logging

    logger = logging.getLogger("knowledge_os.retrieval.orchestrator")
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "propagate", True)


class _BrokenLexical(FakeLexicalStore):
    async def search(self, *, query, top_k, query_filter):
        raise RuntimeError("lexical down")


async def test_missing_leg_degrades(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _enable_orchestrator_logger(monkeypatch)
    dense = FakeVectorStore(dimension=DIMENSION)
    await dense.ensure_collection(collection=COLLECTION, dimension=DIMENSION)
    await dense.upsert(collection=COLLECTION, records=_records())
    orchestrator = HybridRetrievalOrchestrator(
        dense=dense,
        embedding=FakeEmbeddingProvider(),
        lexical=_BrokenLexical(),
        collection=COLLECTION,
    )
    with caplog.at_level("WARNING"):
        items = await orchestrator.retrieve(
            query="hybrid retrieval evidence",
            acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"]),
        )
    assert items, "dense leg alone must still produce results"
    assert "retrieval_leg_failed" in caplog.text


async def test_store_search_receives_the_scope_filter() -> None:
    orchestrator, dense, _ = await _orchestrator()
    await orchestrator.retrieve(
        query="hybrid retrieval", acl_scope=ACLScope(user_id="u1", workspace_ids=["ws-a"])
    )
    assert dense.search_calls
    assert dense.search_calls[-1] == SearchFilter(workspace_ids=["ws-a"])
