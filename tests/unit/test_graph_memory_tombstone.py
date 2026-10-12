"""Unit tests for MemoryGraphStore tombstone lifecycle.

These tests mirror the SQLite deletion regressions for the in-memory adapter,
covering entity, relationship, and document deletion plus the unresolved-ID
fallback state where community member IDs exist but cannot be resolved to names.
"""

from __future__ import annotations

import pytest
from graph import (
    Entity,
    EntityType,
    GraphCommunity,
    GraphRetriever,
    MemoryGraphStore,
    Relationship,
    RelationshipType,
)


def _ent(eid: str, ws: str, name: str, did: str = "d-1") -> Entity:
    """Build a PERSON entity for tests."""
    return Entity(
        id=eid,
        name=name,
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=[f"c-{eid}"],
        document_ids=[did],
    )


def _rel(rid: str, ws: str, src: str, tgt: str, did: str = "d-1") -> Relationship:
    """Build a RELATED_TO relationship for tests."""
    return Relationship(
        id=rid,
        source_entity_id=src,
        target_entity_id=tgt,
        type=RelationshipType.RELATED_TO,
        workspace_id=ws,
        chunk_ids=[f"c-{src}", f"c-{tgt}"],
        document_ids=[did],
    )


@pytest.mark.unit
async def test_memory_tombstone_entity_regenerates_partial_community_summary() -> None:
    """Deleting one entity from a community regenerates summary, omits stale name."""

    store = MemoryGraphStore()
    ws = "ws-mem"
    await store.ensure_graph(ws)
    e1 = _ent("e1", ws, "Alice")
    e2 = _ent("e2", ws, "Bob")
    r1 = _rel("r1", ws, "e1", "e2")
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Alice and Bob",
        summary="Alice and Bob are connected.",
        entity_ids=["e1", "e2"], relationship_ids=["r1"],
    )
    await store.upsert_entities(ws, [e1, e2])
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [c1])

    hit = await store.tombstone_entity(ws, "e1")
    assert hit is True
    assert await store.get_entity(ws, "e1") is None
    assert await store.get_entity(ws, "e2") is not None

    comms = await store.get_communities(ws)
    assert len(comms) == 1
    assert "e1" not in comms[0].entity_ids
    assert "e2" in comms[0].entity_ids
    assert "Alice" not in comms[0].summary
    assert "Bob" in comms[0].summary

    retriever = GraphRetriever(store)
    gres = await retriever.global_search(ws, "Alice")
    assert gres.evidence_chunk_ids == []
    assert not any("Alice" in c.summary for c in gres.communities)


@pytest.mark.unit
async def test_memory_tombstone_relationship_invalidates_edge_only_community_summary() -> None:
    """Edge-only invalidation: deleting a relationship regenerates summaries.

    Both entities survive; only the edge provenance is removed.  The community
    summary must be regenerated from entity names and must not retain the
    original relationship fact text.
    """

    store = MemoryGraphStore()
    ws = "ws-mem-edge"
    await store.ensure_graph(ws)
    e1 = _ent("e1", ws, "Alice")
    e2 = _ent("e2", ws, "Bob")
    r1 = _rel("r1", ws, "e1", "e2")
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Alice and Bob",
        summary="Alice and Bob are connected.",
        entity_ids=["e1", "e2"], relationship_ids=["r1"],
    )
    await store.upsert_entities(ws, [e1, e2])
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [c1])

    hit = await store.tombstone_relationship(ws, "r1")
    assert hit is True
    assert await store.get_entity(ws, "e1") is not None
    assert await store.get_entity(ws, "e2") is not None

    comms = await store.get_communities(ws)
    assert len(comms) == 1
    assert comms[0].relationship_ids == []
    assert comms[0].entity_ids == ["e1", "e2"]
    # Removed fact text must be absent.
    assert "connected" not in comms[0].summary
    assert "connected" not in comms[0].title
    # Surviving entity names present in regenerated summary/title.
    assert "Alice" in comms[0].summary or "Bob" in comms[0].summary

    retriever = GraphRetriever(store)
    gres = await retriever.global_search(ws, "connected")
    assert gres.evidence_chunk_ids == []
    assert not any("connected" in c.summary for c in gres.communities)


@pytest.mark.unit
async def test_memory_tombstone_document_invalidates_all_community_provenance() -> None:
    """Tombstoning a document removes all its entities/edges and invalidates communities."""

    store = MemoryGraphStore()
    ws = "ws-mem-doc"
    await store.ensure_graph(ws)
    e1 = _ent("e1", ws, "Alice", did="d-1")
    r1 = Relationship(
        id="r1", source_entity_id="e1", target_entity_id="e1",
        type=RelationshipType.RELATED_TO, workspace_id=ws,
        chunk_ids=["c-1"], document_ids=["d-1"],
    )
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Alice Team",
        summary="Alice is the sole member.",
        entity_ids=["e1"], relationship_ids=["r1"],
    )
    await store.upsert_entities(ws, [e1])
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [c1])

    removed = await store.tombstone_document(ws, "d-1")
    assert removed == 2  # e1 + r1

    assert await store.get_entity(ws, "e1") is None
    assert await store.get_communities(ws) == []

    retriever = GraphRetriever(store)
    gres = await retriever.global_search(ws, "Alice")
    assert gres.communities == []
    assert gres.evidence_chunk_ids == []


@pytest.mark.unit
async def test_memory_tombstone_document_preserves_surviving_member() -> None:
    """Partial document deletion keeps surviving entity provenance."""

    store = MemoryGraphStore()
    ws = "ws-mem-partial"
    await store.ensure_graph(ws)
    e1 = _ent("e1", ws, "Alice", did="d-1")
    e2 = _ent("e2", ws, "Bob", did="d-2")
    r1 = Relationship(
        id="r1", source_entity_id="e1", target_entity_id="e2",
        type=RelationshipType.RELATED_TO, workspace_id=ws,
        chunk_ids=["c-1", "c-2"], document_ids=["d-1", "d-2"],
    )
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Alice and Bob",
        summary="Alice and Bob are on the team.",
        entity_ids=["e1", "e2"], relationship_ids=["r1"],
    )
    await store.upsert_entities(ws, [e1, e2])
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [c1])

    removed = await store.tombstone_document(ws, "d-1")  # e1 + r1
    assert removed == 2

    comms = await store.get_communities(ws)
    assert len(comms) == 1
    assert "e1" not in comms[0].entity_ids
    assert "e2" in comms[0].entity_ids
    assert "Alice" not in comms[0].summary
    assert "Bob" in comms[0].summary
    assert await store.get_entity(ws, "e2") is not None

    retriever = GraphRetriever(store)
    gres = await retriever.global_search(ws, "Alice")
    assert gres.evidence_chunk_ids == []
    assert not any("Alice" in c.summary for c in gres.communities)


@pytest.mark.unit
async def test_memory_tombstone_relationship_missing_id_returns_false() -> None:
    """Tombstoning a non-existent relationship returns False, no state change."""

    store = MemoryGraphStore()
    ws = "ws-mem-missing"
    await store.ensure_graph(ws)
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Team",
        summary="Alice and Bob.",
        entity_ids=["e1", "e2"], relationship_ids=["r99"],
    )
    await store.upsert_communities(ws, [c1])

    hit = await store.tombstone_relationship(ws, "r99")
    assert hit is False
    # Community must be unchanged.
    comms = await store.get_communities(ws)
    assert len(comms) == 1
    assert comms[0].relationship_ids == ["r99"]
    assert "Alice" in comms[0].summary


@pytest.mark.unit
async def test_memory_tombstone_entity_missing_id_returns_false() -> None:
    """Tombstoning a non-existent entity returns False."""

    store = MemoryGraphStore()
    ws = "ws-mem-missing-ent"
    await store.ensure_graph(ws)
    hit = await store.tombstone_entity(ws, "nonexistent")
    assert hit is False


@pytest.mark.unit
async def test_memory_tombstone_document_unresolved_ids_invalidates_community() -> None:
    """Fallback: community member IDs exist but cannot be loaded → invalidated.

    A relationship referencing document d-1 is written, but its source/target
    entities are never inserted.  A community references the missing entity ID
    and the relationship ID with a stale summary.  Tombstoning the document
    invalidates the relationship, and since no entity names can be loaded for
    surviving member IDs, the community must be deleted rather than kept with
    its original stale summary.
    """

    store = MemoryGraphStore()
    ws = "ws-mem-fallback"
    await store.ensure_graph(ws)
    r1 = Relationship(
        id="r1", source_entity_id="missing-src", target_entity_id="missing-tgt",
        type=RelationshipType.RELATED_TO, workspace_id=ws,
        chunk_ids=["c-1"], document_ids=["d-1"],
    )
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Deleted acquisition",
        summary="Secret acquisition happened.",
        entity_ids=["missing-src"], relationship_ids=["r1"],
    )
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [c1])

    removed = await store.tombstone_document(ws, "d-1")
    assert removed >= 1  # at least the relationship r1

    # Community must be invalidated — stale summary must not surface.
    comms = await store.get_communities(ws)
    assert len(comms) == 0

    retriever = GraphRetriever(store)
    gres = await retriever.global_search(ws, "Secret")
    assert gres.evidence_chunk_ids == []
    assert not any("Secret" in c.summary for c in gres.communities)
