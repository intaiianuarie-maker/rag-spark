"""Measured retrieval stages distinguish empty work from failure or omission."""

from __future__ import annotations

import asyncio
import json
import math

import pytest
from answer.models import Answer
from answer.tracing import _RetrievalObserver, _Trace
from core.auth import ACLScope
from retrieval.interfaces import RetrievalMode, RetrievedItem
from retrieval.orchestrator import (
    HybridRetrievalOrchestrator,
    RetrievalResult,
    RetrievalStatus,
    retrieval_outcome_ctx,
)

pytestmark = pytest.mark.unit


def _item(chunk="dense", workspace="A"):
    return RetrievedItem(chunk_id=chunk, document_id="doc", workspace_id=workspace,
                         content="private source content", score=0.8)


class Embedder:
    model_name = "fixture-embedding"
    revision = "fixture-1"
    dimension = 2

    async def embed_query(self, query):
        await asyncio.sleep(0)
        return [1.0, 0.0]


class Store:
    def __init__(self, items=None, fail=False):
        self.items = items or []
        self.fail = fail
        self.calls = 0

    async def search(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("private query or vendor details")
        return self.items


class Reranker:
    async def rerank(self, *, documents, top_k, **kwargs):
        await asyncio.sleep(0)
        return documents[:top_k]


def _orchestrator(lexical=None, **kwargs):
    return HybridRetrievalOrchestrator(
        dense=Store([_item()]), embedding=Embedder(), lexical=lexical or Store(),
        collection="fixture", **kwargs,
    )


def _completed(stage, count):
    assert stage["status"] == "completed"
    assert stage["candidate_count"] == count
    assert math.isfinite(stage["latency_ms"]) and stage["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_empty_lexical_completion_is_measured_without_fabricating_item_score():
    lexical = Store()
    result = await _orchestrator(lexical, reranker=Reranker()).retrieve_result(
        query="private query", acl_scope=ACLScope(workspace_ids=["A"])
    )
    assert lexical.calls == 1
    assert result.status is RetrievalStatus.OK
    _completed(result.stages["lexical"], 0)
    _completed(result.stages["dense"], 1)
    _completed(result.stages["fusion"], 1)
    _completed(result.stages["rerank"], 1)
    _completed(result.stages["context"], 1)
    assert result.stages["embedding"]["status"] == "completed"
    assert result.stages["embedding"]["candidate_count"] is None
    assert result.stages["lexical"]["returned_context_count"] == 0
    assert "lexical" not in result.items[0].scores
    assert set(result.items[0].scores) == {"dense", "rrf", "rerank"}
    assert "private" not in json.dumps(result.stages)


@pytest.mark.asyncio
async def test_semantic_mode_does_not_label_unexecuted_lexical_as_empty_completed():
    lexical = Store()
    result = await _orchestrator(lexical).retrieve_result(
        query="question", acl_scope=ACLScope(workspace_ids=["A"]), mode=RetrievalMode.SEMANTIC
    )
    assert lexical.calls == 0
    assert result.stages["lexical"] == {
        "status": "not_run", "candidate_count": None,
        "latency_ms": None, "returned_context_count": 0,
    }


@pytest.mark.asyncio
async def test_lexical_failure_is_measured_failed_not_empty_and_remains_fail_closed():
    result = await _orchestrator(Store(fail=True)).retrieve_result(
        query="private query", acl_scope=ACLScope(workspace_ids=["A"])
    )
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.items == []
    lexical = result.stages["lexical"]
    assert lexical["status"] == "failed" and lexical["candidate_count"] is None
    assert lexical["latency_ms"] >= 0
    assert result.stages["fusion"]["status"] == "not_run"
    assert "private" not in json.dumps(result.stages)


@pytest.mark.asyncio
async def test_disabled_optional_stages_are_not_configured_and_never_timed():
    result = await _orchestrator().retrieve_result(
        query="question", acl_scope=ACLScope(workspace_ids=["A"]),
        mode=RetrievalMode.HYBRID_GRAPH,
    )
    for stage in ("sparse", "graph", "rerank"):
        assert result.stages[stage]["status"] == "not_configured"
        assert result.stages[stage]["candidate_count"] is None
        assert result.stages[stage]["latency_ms"] is None


@pytest.mark.asyncio
async def test_counts_filter_foreign_rows_and_distinguish_context_truncation():
    orchestrator = _orchestrator(Store([_item("lexical"), _item("foreign", "B")]))
    result = await orchestrator.retrieve_result(
        query="question", acl_scope=ACLScope(workspace_ids=["A"]), context_top_k=1
    )
    _completed(result.stages["lexical"], 1)  # foreign store output never counts
    assert [item.chunk_id for item in result.items] == ["dense"]
    assert result.stages["lexical"]["returned_context_count"] == 0
    assert result.stages["dense"]["returned_context_count"] == 1
    assert result.stages["fusion"]["candidate_count"] == 2
    assert result.stages["fusion"]["returned_context_count"] == 1
    assert "lexical" not in result.items[0].scores


@pytest.mark.asyncio
async def test_embedding_failure_does_not_pretend_dense_ran():
    class FailingEmbedder(Embedder):
        async def embed_query(self, query):
            raise RuntimeError("private embedding error")

    orchestrator = _orchestrator()
    orchestrator.embedding = FailingEmbedder()
    result = await orchestrator.retrieve_result(
        query="question", acl_scope=ACLScope(workspace_ids=["A"])
    )
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.stages["embedding"]["status"] == "failed"
    assert result.stages["embedding"]["latency_ms"] >= 0
    assert result.stages["dense"]["status"] == "not_run"
    _completed(result.stages["lexical"], 0)


@pytest.mark.asyncio
async def test_empty_scope_attempts_no_stage_and_has_no_invented_timing():
    result = await _orchestrator().retrieve_result(query="question", acl_scope=ACLScope())
    assert len(result.stages) == 8
    for stage in result.stages.values():
        assert stage["status"] == "not_run"
        assert stage["candidate_count"] is stage["latency_ms"] is None


@pytest.mark.asyncio
async def test_real_sparse_and_graph_outputs_have_measured_eligible_counts():
    class Graph:
        async def local_search(self, **kwargs):
            return [_item("graph")]

        async def global_search(self, **kwargs):
            return []

        def to_retrieved_items(self, result, **kwargs):
            return result

    result = await _orchestrator(sparse=Store([_item("sparse")]), graph=Graph()).retrieve_result(
        query="question", acl_scope=ACLScope(workspace_ids=["A"]),
        mode=RetrievalMode.HYBRID_GRAPH,
    )
    _completed(result.stages["sparse"], 1)
    _completed(result.stages["graph"], 1)
    assert result.stages["sparse"]["returned_context_count"] == 1
    assert result.stages["graph"]["returned_context_count"] == 1


@pytest.mark.asyncio
async def test_concurrent_observers_copy_only_their_call_metadata_without_text():
    class QueryStore(Store):
        async def search(self, *, query, **kwargs):
            await asyncio.sleep(0)
            return [] if query.endswith("A") else [_item("lexical")]

    orchestrator = _orchestrator(QueryStore())

    async def observe(query):
        trace = _Trace(query=query, scope=ACLScope(workspace_ids=["A"]),
                       mode=RetrievalMode.HYBRID)
        await _RetrievalObserver(orchestrator, trace).retrieve(query=query, acl_scope=trace.scope)
        outcome = retrieval_outcome_ctx.get()
        assert trace.retrieval_stages == outcome.stages
        assert trace.retrieval_stages is not outcome.stages
        payload = trace.payload(Answer(answer="fixture answer"))
        assert payload["retrieval"]["stages"] == trace.retrieval_stages
        assert "private" not in json.dumps(payload)
        payload["retrieval"]["stages"]["lexical"]["candidate_count"] = 777
        assert trace.retrieval_stages["lexical"]["candidate_count"] != 777
        return trace

    a, b = await asyncio.gather(observe("private query A"), observe("private query B"))
    assert a.retrieval_stages["lexical"]["candidate_count"] == 0
    assert b.retrieval_stages["lexical"]["candidate_count"] == 1
    assert "private" not in json.dumps(a.retrieval_stages)
    a.retrieval_stages["lexical"]["candidate_count"] = 999
    assert b.retrieval_stages["lexical"]["candidate_count"] == 1


@pytest.mark.asyncio
async def test_delegate_without_diagnostics_cannot_inherit_previous_stage_metadata():
    class Uninstrumented:
        async def retrieve(self, **kwargs):
            return []

    retrieval_outcome_ctx.set(RetrievalResult(items=[], stages={"lexical": {"status": "failed"}}))
    trace = _Trace(query="Q", scope=ACLScope(workspace_ids=["A"]), mode=RetrievalMode.HYBRID)
    await _RetrievalObserver(Uninstrumented(), trace).retrieve(query="Q")
    assert trace.retrieval_stages == {}


@pytest.mark.asyncio
async def test_hydration_drops_reconcile_only_returned_counts_without_inventing_stage_evidence():
    orchestrator = _orchestrator(Store([_item("lexical")]))

    class HydratingWrapper:
        async def retrieve(self, **kwargs):
            items = await orchestrator.retrieve(**kwargs)
            # Canonical hydration can remove a row after its base retrieval.
            return [item for item in items if item.chunk_id != "lexical"]

    trace = _Trace(query="Q", scope=ACLScope(workspace_ids=["A"]), mode=RetrievalMode.HYBRID)
    await _RetrievalObserver(HydratingWrapper(), trace).retrieve(query="Q", acl_scope=trace.scope)
    upstream = retrieval_outcome_ctx.get()
    assert upstream.stages["lexical"]["returned_context_count"] == 1
    assert trace.retrieval_stages["lexical"]["returned_context_count"] == 0
    assert [item["chunk_id"] for item in trace.items] == ["dense"]
    for name, stage in trace.retrieval_stages.items():
        for field in ("status", "candidate_count", "latency_ms"):
            assert stage[field] == upstream.stages[name][field]
    assert trace.retrieval_stages["fusion"]["returned_context_count"] == 1
    assert trace.retrieval_stages["context"]["returned_context_count"] == 1
