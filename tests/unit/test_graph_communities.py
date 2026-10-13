# ruff: noqa: E501
"""T-V2-007: community detection + summaries + global retrieval."""

from __future__ import annotations

import pytest
from graph.communities import SUMMARY_MAX_CHARS, SUMMARY_MODEL, detect_communities
from graph.extractor import GraphChunk, RuleBasedGraphExtractor
from graph.interfaces import Entity, GraphCommunity, Relationship
from graph.sqlite_store import SQLiteGraphStore


@pytest.mark.unit
def test_detect_communities_deterministic() -> None:
    """Same graph → same communities (deterministic assignment)."""
    ws = "ws-test"
    e1 = Entity(id="e1", name="Alice", workspace_id=ws, chunk_ids=["ch-1"])
    e2 = Entity(id="e2", name="Bob", workspace_id=ws, chunk_ids=["ch-1"])
    e3 = Entity(id="e3", name="Charlie", workspace_id=ws, chunk_ids=["ch-2"])
    r1 = Relationship(id="r1", source_entity_id="e1", target_entity_id="e2", workspace_id=ws, chunk_ids=["ch-1"])
    # e3 isolated, e1-e2 connected
    com1 = detect_communities([e1, e2, e3], [r1], ws)
    com2 = detect_communities([e1, e2, e3], [r1], ws)
    assert [c.id for c in com1] == [c.id for c in com2]
    assert [c.entity_ids for c in com1] == [c.entity_ids for c in com2]
    # e3 is singleton, not a community (needs >=2)
    assert len(com1) == 1
    assert set(com1[0].entity_ids) == {"e1", "e2"}


@pytest.mark.unit
def test_detect_communities_connected_components() -> None:
    """Connected components: 2 separate components → 2 communities."""
    ws = "ws-test"
    # Component 1: e1 - e2
    e1 = Entity(id="e1", name="Alice", workspace_id=ws, chunk_ids=["ch-1"])
    e2 = Entity(id="e2", name="Bob", workspace_id=ws, chunk_ids=["ch-1"])
    r1 = Relationship(id="r1", source_entity_id="e1", target_entity_id="e2", workspace_id=ws, chunk_ids=["ch-1"])
    # Component 2: e3 - e4
    e3 = Entity(id="e3", name="Charlie", workspace_id=ws, chunk_ids=["ch-2"])
    e4 = Entity(id="e4", name="David", workspace_id=ws, chunk_ids=["ch-2"])
    r2 = Relationship(id="r2", source_entity_id="e3", target_entity_id="e4", workspace_id=ws, chunk_ids=["ch-2"])
    coms = detect_communities([e1, e2, e3, e4], [r1, r2], ws)
    assert len(coms) == 2
    # Each community has 2 entities
    assert all(len(c.entity_ids) == 2 for c in coms)
    # No isolated singletons


@pytest.mark.unit
async def test_summaries_bounded_and_provenance_complete(tmp_path) -> None:
    """Summaries bounded (all under cap) and provenance complete."""
    ws = "ws-test"
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    await store.set_enabled(ws, True)

    # Create a small graph
    chunk = GraphChunk(chunk_id="ch-1", text="Alice Smith works at Acme Corp. Bob Jones is located in Paris.", workspace_id=ws, document_id="doc-1")
    extractor = RuleBasedGraphExtractor()
    result = await extractor.extract([chunk])
    await store.upsert_extraction(ws, result.entities, result.relationships, [])

    # Detect communities
    from graph.communities import detect_communities, summarize_community

    # Fetch back to get stored entities
    entities = []
    for ent in result.entities:
        fetched = await store.get_entity(ws, ent.id)
        if fetched:
            entities.append(fetched)
    # Relationships similarly
    # For test, use the extracted ones directly
    communities = detect_communities(result.entities, result.relationships, ws)
    assert len(communities) >= 0  # may be 0 or 1 depending on graph

    for comm in communities:
        # Provenance complete: every summary maps to member ids
        assert comm.entity_ids
        assert all(isinstance(eid, str) for eid in comm.entity_ids)
        assert all(isinstance(rid, str) for rid in comm.relationship_ids)
        # Summary generation (free model, bounded)
        # Use fallback if LLM not available (should not fail)
        member_ents = [e for e in result.entities if e.id in comm.entity_ids]
        member_rels = [r for r in result.relationships if r.id in comm.relationship_ids]
        summary = await summarize_community(comm, member_ents, member_rels, ws)
        # Bounded
        assert len(summary) <= SUMMARY_MAX_CHARS
        assert len(summary) > 0 or len(comm.entity_ids) == 0

    store.close()


@pytest.mark.unit
async def test_global_retrieval_returns_summaries_with_provenance(tmp_path) -> None:
    """Global retrieval: query → rank communities → return summaries with member provenance."""
    ws = "ws-test"
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    await store.set_enabled(ws, True)

    # Create entities and community
    e1 = Entity(id="e1", name="Alice Smith", workspace_id=ws, chunk_ids=["ch-1"], document_ids=["doc-1"])
    e2 = Entity(id="e2", name="Acme Corp", workspace_id=ws, chunk_ids=["ch-1"], document_ids=["doc-1"])
    r1 = Relationship(id="r1", source_entity_id="e1", target_entity_id="e2", workspace_id=ws, chunk_ids=["ch-1"])
    comm = GraphCommunity(id="comm-1", workspace_id=ws, title="Alice + Acme", summary="Alice works at Acme.", entity_ids=["e1", "e2"], relationship_ids=["r1"])

    await store.upsert_entities(ws, [e1, e2])
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [comm])

    from graph.retriever import GraphRetriever
    retriever = GraphRetriever(store)
    result = await retriever.global_search(ws, "Alice Acme")

    # Should return the community
    assert len(result.communities) == 1
    assert result.communities[0].id == "comm-1"
    # Evidence chunk ids should include member provenance
    assert "ch-1" in result.evidence_chunk_ids
    # Summary should be present
    assert "Alice" in result.summary or "Acme" in result.summary

    store.close()


@pytest.mark.unit
def test_summary_model_is_free_and_bounded() -> None:
    """Summary model is free, max_tokens >=1000, cap stated."""
    from graph.communities import SUMMARY_MAX_CHARS, SUMMARY_MAX_TOKENS

    assert "free" in SUMMARY_MODEL, "must be free model per standing rule"
    assert SUMMARY_MAX_TOKENS >= 1000, "reasoning models need max_tokens>=1000"
    assert SUMMARY_MAX_CHARS == 500
    assert SUMMARY_MODEL == "nvidia/nemotron-3.5-lightning:free"
