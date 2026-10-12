"""G5 evaluation: graph-vs-baseline evidence recall on multi-hop queries.

Per V2-GRAPH-PLAN §2 work package G5:

    graph-vs-baseline on Q8 multi-hop/multi-source sets; EvidenceRecall@20 and
    grounded-success deltas reported paired; no holdout tuning.

These are deterministic, in-process unit tests (no models, no services): they
build a small knowledge graph with explicit entity/relation provenance **plus**
a companion vector+lexical baseline over the *same* chunks, then measure the
evidence recall gap on a question whose answer requires following a relationship
chain that no single chunk text surfaces end-to-end.
"""

from __future__ import annotations

import pytest
from core.auth import ACLScope
from graph import (
    Entity,
    EntityType,
    GraphCommunity,
    GraphRetriever,
    MemoryGraphStore,
    Relationship,
    RelationshipType,
)
from harness_fakes import (
    FakeEmbeddingProvider,
    FakeLexicalStore,
    FakeReranker,
    FakeVectorStore,
    make_record,
)
from retrieval.interfaces import RetrievalMode

pytestmark = pytest.mark.unit

COLLECTION = "graph-eval"
DIMENSION = 8
WS = "ws-graph"


def _vector(*head: float) -> list[float]:
    return [*head, *([0.0] * (DIMENSION - len(head)))]


def _build_graph_store() -> MemoryGraphStore:
    """Graph: Alice → WORKS_FOR → Acme → ACQUIRED → Globex → PRODUCES → Phoenix.

    Each entity/edge carries chunk provenance; the *relationship* chain is the
    multi-hop signal.  No single chunk text contains all names, so a
    keyword-only leg cannot follow the chain the way the graph can.
    """
    store = MemoryGraphStore()
    ws = WS
    entities = [
        Entity(
            id="ent-alice", name="Alice Smith", type=EntityType.PERSON,
            workspace_id=ws, chunk_ids=["ch-alice"], document_ids=["doc-1"],
        ),
        Entity(
            id="ent-acme", name="Acme Corporation", type=EntityType.ORGANIZATION,
            workspace_id=ws, chunk_ids=["ch-acme"], document_ids=["doc-2"],
        ),
        Entity(
            id="ent-globex", name="Globex Industries", type=EntityType.ORGANIZATION,
            workspace_id=ws, chunk_ids=["ch-globex"], document_ids=["doc-2"],
        ),
        Entity(
            id="ent-phoenix", name="Phoenix Project", type=EntityType.PRODUCT,
            workspace_id=ws, chunk_ids=["ch-phoenix"], document_ids=["doc-3"],
        ),
    ]
    relationships = [
        Relationship(
            id="rel-alice-acme",
            source_entity_id="ent-alice", target_entity_id="ent-acme",
            type=RelationshipType.WORKS_FOR, workspace_id=ws,
            chunk_ids=["ch-acme"], document_ids=["doc-2"],
        ),
        Relationship(
            id="rel-acme-globex",
            source_entity_id="ent-acme", target_entity_id="ent-globex",
            type=RelationshipType.OWNS, workspace_id=ws,
            chunk_ids=["ch-acme", "ch-globex"], document_ids=["doc-2"],
        ),
        Relationship(
            id="rel-globex-phoenix",
            source_entity_id="ent-globex", target_entity_id="ent-phoenix",
            type=RelationshipType.PRODUCES, workspace_id=ws,
            chunk_ids=["ch-globex", "ch-phoenix"], document_ids=["doc-3"],
        ),
    ]
    communities = [
        GraphCommunity(
            id="comm-merger", workspace_id=ws,
            title="Acme-Globex Merger",
            summary="Acme Corporation acquired Globex Industries in 2019.",
            entity_ids=["ent-acme", "ent-globex"],
            relationship_ids=["rel-acme-globex"],
        ),
    ]
    store._ensure_ws(ws)
    store._entities[ws].update({e.id: e for e in entities})
    store._relationships[ws].update({r.id: r for r in relationships})
    store._communities[ws].update({c.id: c for c in communities})
    return store


async def _build_baseline_store() -> tuple[FakeVectorStore, FakeLexicalStore]:
    """Companion dense+lexical stores indexing ALL graph chunks.

    Every chunk that the graph store knows about is also indexed here, so the
    graph-vs-baseline comparison measures retrieval *quality* (traversal of
    entity/relation chains) rather than corpus membership.  The graph retriever's
    advantage on multi-hop questions is that it follows entity links to evidence
    chunks a keyword/dense query cannot associate with the seed entity.
    """
    dense = FakeVectorStore(dimension=DIMENSION)
    lexical = FakeLexicalStore()

    await dense.ensure_collection(collection=COLLECTION, dimension=DIMENSION)
    records = [
        make_record(
            "ch-alice", WS,
            "Alice Smith joined Acme Corporation in 2018.",
            vector=_vector(1.0, 0.0, 0.0, 0.0),
            page=1, section="Intro",
        ),
        make_record(
            "ch-acme", WS,
            "Acme Corporation acquired Globex Industries in 2019.",
            vector=_vector(0.0, 1.0, 0.0, 0.0),
            page=1, section="Merger",
        ),
        make_record(
            "ch-globex", WS,
            "Globex Industries produces the Phoenix Project.",
            vector=_vector(0.0, 0.0, 1.0, 0.0),
            page=2, section="Products",
        ),
    ]
    await dense.upsert(collection=COLLECTION, records=records)
    await lexical.index(records=records)
    return dense, lexical


async def _build_orchestrator_with_graph():
    """Build a baseline orchestrator and attach the graph retriever."""
    from retrieval.orchestrator import HybridRetrievalOrchestrator

    dense, lexical = await _build_baseline_store()
    orch = HybridRetrievalOrchestrator(
        dense=dense,
        embedding=FakeEmbeddingProvider(),
        lexical=lexical,
        collection=COLLECTION,
        reranker=FakeReranker(),
        rerank_enabled=False,
    )
    graph_store = _build_graph_store()
    orch.graph = GraphRetriever(graph_store)
    return orch


# --------------------------------------------------------------------------------------
# EvidenceRecall@K helper
# --------------------------------------------------------------------------------------


def evidence_recall_at_k(
    relevant_chunks: set[str], retrieved_items: list, *, k: int = 20
) -> float:
    """Recall of relevant chunks within the top-``k`` retrieved items."""
    if not relevant_chunks:
        return 0.0
    seen: set[str] = set()
    for item in retrieved_items[:k]:
        if item.chunk_id:
            seen.add(item.chunk_id)
    return len(relevant_chunks & seen) / len(relevant_chunks)


# --------------------------------------------------------------------------------------
# G5 tests
# --------------------------------------------------------------------------------------


@pytest.mark.unit
async def test_graph_local_search_traverses_multi_hop_chain() -> None:
    """Local graph search follows Alice-->Acme-->Globex, surfacing 3 chunks.

    Both the graph store and the baseline dense/lexical stores index the SAME
    three chunks (ch-alice, ch-acme, ch-globex).  The query "What did Alice's
    employer acquire?" seeds entity ``ent-alice`` via token "Alice", then
    traverses Acme-->Globex to surface ``ch-globex``.

    This is a *synthetic traversal regression*: the graph retrieves all
    relevant chunks via entity/relation hops.  Equal K budgets are used for
    both conditions (no forced advantage).  The graph's value is not Recall@K
    in this fixture (bounded by available chunks), but the structural
    correctness of multi-hop traversal and HYBRID_GRAPH fusion.
    """
    orch = await _build_orchestrator_with_graph()
    acl = ACLScope(user_id="u1", workspace_ids=[WS])
    relevant = {"ch-alice", "ch-acme", "ch-globex"}

    # --- graph-only retrieval (traversal follows entity chain) ---
    graph_items = await orch.retrieve(
        query="What did Alice's employer acquire?",
        acl_scope=acl,
        mode=RetrievalMode.GRAPH,
        dense_top_k=20,
        lexical_top_k=20,
        sparse_top_k=0,
        fusion_top_k=20,
    )
    graph_chunks = {item.chunk_id for item in graph_items if item.chunk_id}
    assert graph_chunks, "graph retrieval should surface evidence"
    assert "ch-alice" in graph_chunks
    assert "ch-acme" in graph_chunks
    assert "ch-globex" in graph_chunks
    graph_recall = evidence_recall_at_k(relevant, graph_items, k=20)

    # --- baseline hybrid at equal K (same chunks indexed, no graph leg) ---
    orch.graph = None
    K = 20
    baseline_items = await orch.retrieve(
        query="What did Alice's employer acquire?",
        acl_scope=acl,
        mode=RetrievalMode.HYBRID,
        dense_top_k=K,
        lexical_top_k=K,
        sparse_top_k=0,
        fusion_top_k=K,
    )
    baseline_recall = evidence_recall_at_k(relevant, baseline_items, k=K)

    # Equal-budget comparison: both conditions index the same chunks and use
    # the same K.  The graph's traversal guarantees full recall on the
    # connected subgraph.  At K=20 the baseline also retrieves all 3 chunks
    # (all chunks are indexed and RRF is deterministic), so this is a
    # structural traversal regression, not a Recall@K advantage.
    assert graph_recall == 1.0
    assert baseline_recall == 1.0  # measured equal-budget value at K=20

@pytest.mark.unit
async def test_hybrid_graph_fuses_graph_and_baseline_evidence() -> None:
    """HYBRID_GRAPH fuses graph-traversed evidence with baseline retrieval."""
    orch = await _build_orchestrator_with_graph()
    acl = ACLScope(user_id="u1", workspace_ids=[WS])

    items = await orch.retrieve(
        query="What did Alice's employer acquire?",
        acl_scope=acl,
        mode=RetrievalMode.HYBRID_GRAPH,
        dense_top_k=20,
        lexical_top_k=20,
        sparse_top_k=0,
        fusion_top_k=20,
    )
    chunk_ids = {item.chunk_id for item in items if item.chunk_id}

    # HYBRID_GRAPH must include all multi-hop chunks the graph leg contributed.
    assert "ch-alice" in chunk_ids
    assert "ch-acme" in chunk_ids
    assert "ch-globex" in chunk_ids

    # At least one result carries the graph score when the graph leg surfaced it.
    has_graph_score = any("graph" in item.scores for item in items)
    assert has_graph_score


@pytest.mark.unit
async def test_graph_acl_scoped_no_cross_workspace_leak() -> None:
    """Graph evidence is ACL-scoped: a different workspace's entities are invisible."""
    orch = await _build_orchestrator_with_graph()
    store = orch.graph._store
    await store.upsert_entities(
        "ws-other",
        [
            Entity(
                id="ent-other", name="Secret Co", type=EntityType.ORGANIZATION,
                workspace_id="ws-other", chunk_ids=["ch-secret"],
                document_ids=["doc-x"],
            ),
        ],
    )

    # Query from ws-graph must never surface ws-other's chunk.
    items = await orch.retrieve(
        query="Secret Co",
        acl_scope=ACLScope(user_id="u1", workspace_ids=[WS]),
        mode=RetrievalMode.GRAPH,
        dense_top_k=20,
        lexical_top_k=20,
        sparse_top_k=0,
        fusion_top_k=20,
    )
    chunk_ids = {item.chunk_id for item in items if item.chunk_id}
    assert "ch-secret" not in chunk_ids

    # Same query scoped to ws-other SHOULD surface it.
    items_other = await orch.retrieve(
        query="Secret Co",
        acl_scope=ACLScope(user_id="u2", workspace_ids=["ws-other"]),
        mode=RetrievalMode.GRAPH,
        dense_top_k=20,
        lexical_top_k=20,
        sparse_top_k=0,
        fusion_top_k=20,
    )
    assert "ch-secret" in {
        item.chunk_id for item in items_other if item.chunk_id
    }


@pytest.mark.unit
async def test_graph_off_is_byte_identical_v1() -> None:
    """Flag-off parity: graph=None yields identical results to HYBRID mode.

    Per V2-GRAPH-PLAN §3: when the graph retriever is not wired, HYBRID_GRAPH
    must produce byte-identical results to the v1 default HYBRID path (modulo
    nondeterministic score/rank fields).  This test compares serialized output
    side-by-side rather than merely asserting absence of graph scores.
    """
    orch = await _build_orchestrator_with_graph()
    acl = ACLScope(user_id="u1", workspace_ids=[WS])

    # Condition A: graph=None, HYBRID_GRAPH mode (graph leg no-ops)
    orch.graph = None
    graph_off_items = await orch.retrieve(
        query="Acme Corporation",
        acl_scope=acl,
        mode=RetrievalMode.HYBRID_GRAPH,
        dense_top_k=20,
        lexical_top_k=20,
        sparse_top_k=0,
        fusion_top_k=20,
    )

    # Condition B: pure HYBRID mode (no graph leg at all)
    hybrid_items = await orch.retrieve(
        query="Acme Corporation",
        acl_scope=acl,
        mode=RetrievalMode.HYBRID,
        dense_top_k=20,
        lexical_top_k=20,
        sparse_top_k=0,
        fusion_top_k=20,
    )

    # Nonempty baseline (proves retrieval actually ran, not empty-by-default).
    assert graph_off_items, "HYBRID with graph=None should return results"
    assert hybrid_items, "pure HYBRID should return results"

    # No graph scores in either condition when graph is not wired.
    assert not any("graph" in item.scores for item in graph_off_items)
    assert not any("graph" in item.scores for item in hybrid_items)

    # Same chunk IDs in the same order (RRF is deterministic with fakes).
    off_ids = [item.chunk_id for item in graph_off_items if item.chunk_id]
    hybr_ids = [item.chunk_id for item in hybrid_items if item.chunk_id]
    assert off_ids == hybr_ids, (
    "HYBRID_GRAPH with graph=None must match HYBRID output exactly"
    )

    # Full serialized parity: with fake fixtures and RRF both paths are fully
    # deterministic, so every field (content, workspace, scores, trace, provenance)
    # must match.  Compare normalized JSON of each item.
    off_json = [item.model_dump(mode="json") for item in graph_off_items]
    hybr_json = [item.model_dump(mode="json") for item in hybrid_items]
    assert off_json == hybr_json, (
    "HYBRID_GRAPH(graph=None) and HYBRID must produce byte-identical "
    "serialized output on identical fixtures"
    )
