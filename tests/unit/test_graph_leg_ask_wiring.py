"""The ask path runs the graph leg whenever GRAPH_MODE enables it (G3/G4).

Root cause locked here: /ask defaulted to ``mode=hybrid`` and the orchestrator
only runs the graph leg for ``hybrid_graph``/``graph``, so a populated graph
store never contributed. Graph refs must also resolve to live canonical chunks
inside the caller's scope before they enter fusion.
"""

from __future__ import annotations

import asyncio

import pytest
from app.ask.router import AskRequest, AskStreamRequest, resolve_retrieval_mode
from core.auth import ACLScope
from graph.retriever import GraphRetriever
from graph.sqlite_store import SQLiteGraphStore
from retrieval import orchestrator as orch_mod
from retrieval.interfaces import RetrievalMode, RetrievedItem
from retrieval.orchestrator import HybridRetrievalOrchestrator, RetrievalStatus

pytestmark = pytest.mark.unit


# --- mode resolution (router) ------------------------------------------------


@pytest.mark.parametrize("value", ["rule", "llm", " LLM "])
def test_default_ask_mode_includes_graph_when_graph_mode_on(monkeypatch, value):
    monkeypatch.setenv("GRAPH_MODE", value)
    assert resolve_retrieval_mode(AskRequest(query="q")) is RetrievalMode.HYBRID_GRAPH
    assert resolve_retrieval_mode(AskStreamRequest(query="q")) is RetrievalMode.HYBRID_GRAPH


def test_default_ask_mode_unchanged_when_graph_off(monkeypatch):
    monkeypatch.delenv("GRAPH_MODE", raising=False)
    assert resolve_retrieval_mode(AskRequest(query="q")) is RetrievalMode.HYBRID
    monkeypatch.setenv("GRAPH_MODE", "off")
    assert resolve_retrieval_mode(AskRequest(query="q")) is RetrievalMode.HYBRID


def test_invalid_graph_mode_requests_graph_so_omission_is_disclosed(monkeypatch):
    # The factory refuses the typo (graph=None); asking for hybrid_graph makes
    # the orchestrator report graph not_configured + disclosure, never silence.
    monkeypatch.setenv("GRAPH_MODE", "lmm")
    assert resolve_retrieval_mode(AskRequest(query="q")) is RetrievalMode.HYBRID_GRAPH


def test_explicit_mode_is_respected(monkeypatch):
    monkeypatch.setenv("GRAPH_MODE", "llm")
    assert resolve_retrieval_mode(AskRequest(query="q", mode="hybrid")) is RetrievalMode.HYBRID
    assert resolve_retrieval_mode(AskRequest(query="q", mode="keyword")) is RetrievalMode.KEYWORD


# --- orchestrator graph leg ----------------------------------------------------


def _dense_item():
    return RetrievedItem(
        chunk_id="dense", document_id="d0", workspace_id="A", content="dense text", score=0.9
    )


class Embedder:
    model_name = "fixture"
    revision = "fixture"
    dimension = 2

    async def embed_query(self, query):
        return [1.0, 0.0]


class Store:
    def __init__(self, items=None):
        self.items = items or []

    async def search(self, **kwargs):
        return self.items


class CanonicalLexical(Store):
    """Lexical store exposing the canonical resolver seam (pg_fts.resolve_chunks)."""

    def __init__(self, live: dict[str, str], fail: bool = False):
        super().__init__()
        self.live = live  # chunk_id -> workspace of the LIVE canonical chunk
        self.fail = fail
        self.calls = []

    async def resolve_chunks(self, *, chunk_ids, query_filter):
        self.calls.append((list(chunk_ids), list(query_filter.workspace_ids)))
        if self.fail:
            raise RuntimeError("db down")
        return [
            RetrievedItem(
                chunk_id=cid,
                workspace_id=self.live[cid],
                document_id="doc-" + cid,
                document_version_id="v1",
                content=f"canonical {cid}",
                score=0.0,
                scores={"lexical": 9.0},
                metadata={"filename": "f.txt"},
            )
            for cid in chunk_ids
            if cid in self.live and self.live[cid] in query_filter.workspace_ids
        ]


class Graph:
    def __init__(self, refs=(), fail=False, delay=0.0):
        self.refs = list(refs)
        self.fail = fail
        self.delay = delay

    async def local_search(self, *, workspace_id, query):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("sqlite corrupt")
        return self.refs

    async def global_search(self, *, workspace_id, query):
        return []

    def to_retrieved_items(self, result, *, workspace_id):
        return [
            RetrievedItem(
                chunk_id=c,
                workspace_id=workspace_id,
                content="",
                score=1.0,
                metadata={"is_graph": True, "routing_hint": True},
            )
            for c in result
        ]


def _orch(graph, lexical):
    return HybridRetrievalOrchestrator(
        dense=Store([_dense_item()]),
        embedding=Embedder(),
        lexical=lexical,
        collection="fx",
        graph=graph,
    )


async def _run(orch, mode=RetrievalMode.HYBRID_GRAPH):
    return await orch.retrieve_result(
        query="Who founded Acme?", acl_scope=ACLScope(workspace_ids=["A"]), mode=mode
    )


@pytest.mark.asyncio
async def test_graph_refs_resolve_to_canonical_scoped_chunks_and_fuse():
    # g1 live in A; g2 belongs to workspace B; g3 tombstoned/superseded (not live).
    lexical = CanonicalLexical({"g1": "A", "g2": "B"})
    result = await _run(_orch(Graph(["g1", "g2", "g3"]), lexical))
    assert result.status is RetrievalStatus.OK
    graph = result.stages["graph"]
    assert graph["status"] == "completed"
    assert graph["candidate_count"] == 1
    assert graph["dropped_refs"] == 2
    assert graph["returned_context_count"] == 1
    by_id = {i.chunk_id: i for i in result.items}
    assert set(by_id) == {"dense", "g1"}
    g1 = by_id["g1"]
    assert g1.content == "canonical g1" and g1.document_id == "doc-g1"
    assert g1.metadata["is_graph"] is True  # still a routing hint downstream
    assert set(g1.scores) == {"graph", "rrf"}  # never a fabricated lexical vote
    assert lexical.calls == [(["g1", "g2", "g3"], ["A"])]


@pytest.mark.asyncio
async def test_entity_without_chunk_links_completes_with_zero_candidates():
    result = await _run(_orch(Graph([]), CanonicalLexical({})))
    assert result.status is RetrievalStatus.OK
    assert result.stages["graph"]["status"] == "completed"
    assert result.stages["graph"]["candidate_count"] == 0
    assert [i.chunk_id for i in result.items] == ["dense"]


@pytest.mark.asyncio
async def test_graph_store_failure_degrades_and_dense_still_answers():
    result = await _run(_orch(Graph(fail=True), CanonicalLexical({})))
    assert result.status is RetrievalStatus.DEGRADED
    assert result.stages["graph"]["status"] == "failed"
    assert result.stages["graph"]["candidate_count"] is None
    assert [i.chunk_id for i in result.items] == ["dense"]
    assert result.graph_disclosure


@pytest.mark.asyncio
async def test_graph_timeout_degrades(monkeypatch):
    monkeypatch.setattr(orch_mod, "GRAPH_LEG_TIMEOUT_S", 0.01)
    result = await _run(_orch(Graph(["g1"], delay=1.0), CanonicalLexical({"g1": "A"})))
    assert result.status is RetrievalStatus.DEGRADED
    assert result.stages["graph"]["status"] == "failed"
    assert result.legs_failed[0].error_type == "TimeoutError"
    assert [i.chunk_id for i in result.items] == ["dense"]


@pytest.mark.asyncio
async def test_canonical_resolution_failure_never_passes_unvalidated_refs():
    result = await _run(_orch(Graph(["g1"]), CanonicalLexical({"g1": "A"}, fail=True)))
    assert result.status is RetrievalStatus.DEGRADED
    assert result.stages["graph"]["status"] == "failed"
    assert [i.chunk_id for i in result.items] == ["dense"]


@pytest.mark.asyncio
async def test_corrupt_graph_db_degrades_with_real_store(tmp_path):
    path = tmp_path / "graph.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    store = SQLiteGraphStore(path)
    try:
        result = await _run(_orch(GraphRetriever(store), CanonicalLexical({})))
    finally:
        store.close()
    assert result.status is RetrievalStatus.DEGRADED
    assert result.stages["graph"]["status"] == "failed"
    assert [i.chunk_id for i in result.items] == ["dense"]


@pytest.mark.asyncio
async def test_graph_off_hybrid_trace_unchanged():
    result = await _run(_orch(None, Store()), mode=RetrievalMode.HYBRID)
    assert result.stages["graph"] == {
        "status": "not_run",
        "candidate_count": None,
        "latency_ms": None,
        "returned_context_count": 0,
    }
    assert result.status is RetrievalStatus.OK
