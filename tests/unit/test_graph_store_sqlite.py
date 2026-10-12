"""Unit tests for SQLiteGraphStore lifecycle (issues #2 and #5).

* issue #2 - tombstoning a document invalidates its entities/edges and prunes
  affected communities in the same transaction; global/local retrieval must
  not return stale derived content. Multi-document communities keep their
  surviving provenance.
* issue #5 - the SQLite connection is opened once, closed deterministically
  (close()/context-manager/Exception path) and lazily reopened; schema setup
  runs once per file.
"""

from __future__ import annotations

import pytest
from graph import (
    Entity,
    EntityType,
    GraphCommunity,
    GraphRetriever,
    Relationship,
    RelationshipType,
    SQLiteGraphStore,
)


def _ent(eid: str, ws: str, name: str, did: str) -> Entity:
    return Entity(
        id=eid,
        name=name,
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["c-" + did],
        document_ids=[did],
    )


def _rel(rid: str, ws: str, src: str, tgt: str, did: str) -> Relationship:
    return Relationship(
        id=rid,
        source_entity_id=src,
        target_entity_id=tgt,
        type=RelationshipType.WORKS_FOR,
        workspace_id=ws,
        chunk_ids=["c-" + did],
        document_ids=[did],
    )


@pytest.mark.unit
async def test_sqlite_workspace_boundary_isolation(tmp_path) -> None:
    store = SQLiteGraphStore(tmp_path / "g.db")
    await store.ensure_graph("ws-a")
    await store.ensure_graph("ws-b")

    ea = Entity(id="e1", name="Apple Inc", type=EntityType.ORGANIZATION,
                workspace_id="ws-a", document_ids=["d-1"])
    eb = Entity(id="e1", name="Apple Inc", type=EntityType.ORGANIZATION,
                workspace_id="ws-b", document_ids=["d-1"])
    await store.upsert_entities("ws-a", [ea])
    await store.upsert_entities("ws-b", [eb])

    got_a = await store.get_entity("ws-a", "e1")
    got_b = await store.get_entity("ws-b", "e1")
    assert got_a is not None and got_a.workspace_id == "ws-a"
    assert got_b is not None and got_b.workspace_id == "ws-b"
    found = await store.find_entities_by_name("ws-a", "apple")
    assert len(found) == 1 and found[0].workspace_id == "ws-a"
    store.close()


@pytest.mark.unit
async def test_sqlite_tombstone_document_invalidates_community_and_retrieval(
    tmp_path,
) -> None:
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-x"
    await store.ensure_graph(ws)
    e1, e2 = _ent("e1", ws, "Alice", "d-1"), _ent("e2", ws, "Acme", "d-1")
    e2.type = EntityType.ORGANIZATION
    r1 = _rel("r1", ws, "e1", "e2", "d-1")
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Team", summary="Alice at Acme",
        entity_ids=["e1", "e2"], relationship_ids=["r1"],
    )
    await store.upsert_entities(ws, [e1, e2])
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [c1])

    removed = await store.tombstone_document(ws, "d-1")
    assert removed == 3  # e1, e2, r1
    assert await store.get_entity(ws, "e1") is None
    assert await store.get_entity(ws, "e2") is None
    ents, rels = await store.get_neighbors(ws, "e1")
    assert ents == [] and rels == []
    assert await store.get_communities(ws) == []

    retriever = GraphRetriever(store)
    gres = await retriever.global_search(ws, "Alice")
    assert gres.communities == [] and gres.evidence_chunk_ids == []
    store.close()


@pytest.mark.unit
async def test_sqlite_tombstone_document_preserves_multidoc_community(
    tmp_path,
) -> None:
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-x"
    await store.ensure_graph(ws)
    e1 = Entity(id="e1", name="Alice", type=EntityType.PERSON, workspace_id=ws,
                chunk_ids=["c-1"], document_ids=["d-1"])
    e2 = Entity(id="e2", name="Bob", type=EntityType.PERSON, workspace_id=ws,
                chunk_ids=["c-2"], document_ids=["d-2"])
    r1 = Relationship(
        id="r1", source_entity_id="e1", target_entity_id="e2",
        type=RelationshipType.RELATED_TO, workspace_id=ws,
        chunk_ids=["c-1", "c-2"], document_ids=["d-1", "d-2"],
    )
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Team", summary="Alice and Bob",
        entity_ids=["e1", "e2"], relationship_ids=["r1"],
    )
    await store.upsert_entities(ws, [e1, e2])
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [c1])

    removed = await store.tombstone_document(ws, "d-1")  # e1 + r1
    assert removed == 2
    comms = await store.get_communities(ws)
    assert len(comms) == 1
    assert comms[0].entity_ids == ["e2"]
    assert comms[0].relationship_ids == []
    # Summary/title must be regenerated — must NOT contain deleted entity name.
    assert "Alice" not in comms[0].title
    assert "Alice" not in comms[0].summary
    # Title/summary now reference only the surviving entity.
    assert "Bob" in comms[0].title or "Bob" in comms[0].summary
    assert await store.get_entity(ws, "e2") is not None
    assert await store.get_entity(ws, "e1") is None

    # Global search must not return deleted names after partial deletion.
    retriever = GraphRetriever(store)
    gres = await retriever.global_search(ws, "Alice")
    assert gres.evidence_chunk_ids == []
    assert not any("Alice" in c.summary for c in gres.communities)
    store.close()


@pytest.mark.unit
async def test_sqlite_partial_deletion_regenerates_summary_and_invalidates_community(
    tmp_path,
) -> None:
    """P1 regression: partial deletion must regenerate summaries, not retain stale ones.

    Community "Team" has title="Alice and Bob", summary="Alice and Bob are members"
    over entities e1 (Alice, d-1) and e2 (Bob, d-1).  Deleting only the "Alice"
    entity must remove Alice from the community summary/title AND leave the
    community searchable for "Bob" only.
    """
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-y"
    await store.ensure_graph(ws)
    e1 = Entity(
        id="e1", name="Alice", type=EntityType.PERSON, workspace_id=ws,
        chunk_ids=["c-1"], document_ids=["d-1"],
    )
    e2 = Entity(
        id="e2", name="Bob", type=EntityType.PERSON, workspace_id=ws,
        chunk_ids=["c-2"], document_ids=["d-1"],
    )
    r1 = Relationship(
        id="r1", source_entity_id="e1", target_entity_id="e2",
        type=RelationshipType.RELATED_TO, workspace_id=ws,
        chunk_ids=["c-1", "c-2"], document_ids=["d-1"],
    )
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Alice and Bob",
        summary="Alice and Bob are members of the team.",
        entity_ids=["e1", "e2"], relationship_ids=["r1"],
    )
    await store.upsert_entities(ws, [e1, e2])
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [c1])

    # Tombstone only Alice's entity (edge-only invalidation path).
    hit = await store.tombstone_entity(ws, "e1")
    assert hit is True

    # Entity removed, Bob still present.
    assert await store.get_entity(ws, "e1") is None
    assert await store.get_entity(ws, "e2") is not None

    comms = await store.get_communities(ws)
    assert len(comms) == 1
    # Summary/title regenerated from surviving entity names only.
    assert "Alice" not in comms[0].title
    assert "Alice" not in comms[0].summary
    assert "Alice" not in str(comms[0].entity_ids + comms[0].relationship_ids)
    assert "Bob" in comms[0].title

    # Global search for "Alice" returns nothing.
    retriever = GraphRetriever(store)
    gres = await retriever.global_search(ws, "Alice")
    assert gres.evidence_chunk_ids == []
    store.close()


@pytest.mark.unit
async def test_sqlite_tombstone_relationship_invalidates_community_summary(
    tmp_path,
) -> None:
    """Edge-only invalidation: deleting a relationship regenerates summaries.

    Community 'Team' over entities e1 (Alice) and e2 (Bob) with relationship
    r1 (Alice→Bob).  Titles/summary mention both.  Deleting r1 removes the
    edge from provenance but both entities survive; summary must be
    regenerated to omit the deleted relationship fact.
    """
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-z"
    await store.ensure_graph(ws)
    e1 = Entity(
        id="e1", name="Alice", type=EntityType.PERSON, workspace_id=ws,
        chunk_ids=["c-1"], document_ids=["d-1"],
    )
    e2 = Entity(
        id="e2", name="Bob", type=EntityType.PERSON, workspace_id=ws,
        chunk_ids=["c-2"], document_ids=["d-1"],
    )
    r1 = Relationship(
        id="r1", source_entity_id="e1", target_entity_id="e2",
        type=RelationshipType.RELATED_TO, workspace_id=ws,
        chunk_ids=["c-1", "c-2"], document_ids=["d-1"],
    )
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Alice and Bob",
        summary="Alice and Bob are connected.",
        entity_ids=["e1", "e2"], relationship_ids=["r1"],
    )
    await store.upsert_entities(ws, [e1, e2])
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [c1])

    # Edge-only: tombstone the relationship, entities survive.
    hit = await store.tombstone_relationship(ws, "r1")
    assert hit is True
    assert await store.get_entity(ws, "e1") is not None
    assert await store.get_entity(ws, "e2") is not None

    comms = await store.get_communities(ws)
    assert len(comms) == 1
    assert comms[0].relationship_ids == []  # edge pruned
    assert comms[0].entity_ids == ["e1", "e2"]  # entities still present
    # Summary/title regenerated from surviving entity names ONLY.
    # The removed fact text must be absent (not just surviving names present).
    assert "connected" not in comms[0].summary
    assert "connected" not in comms[0].title
    assert "Alice" in comms[0].title or "Bob" in comms[0].title

    # Global search must not surface the deleted relationship fact.
    retriever = GraphRetriever(store)
    gres = await retriever.global_search(ws, "connected")
    assert gres.evidence_chunk_ids == []
    assert not any("connected" in c.summary for c in gres.communities)
    store.close()


@pytest.mark.unit
async def test_sqlite_tombstone_relationship_unresolved_ids_invalidates_community(
    tmp_path,
) -> None:
    """Fallback: community with unresolved member IDs is tombstoned, not retained.

    A relationship referencing a document is written, but its source/target
    entities are never inserted.  A community references the missing entity ID
    and the relationship ID with a stale summary.  Tombstoning the document
    invalidates the relationship, and since no entity names can be loaded for
    surviving member IDs, the community must be tombstoned rather than kept
    with its original summary.
    """
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-fallback"
    await store.ensure_graph(ws)
    # Relationship with document d-1 but entities that don't exist in the store.
    r1 = Relationship(
        id="r1",
        source_entity_id="missing-src",
        target_entity_id="missing-tgt",
        type=RelationshipType.RELATED_TO,
        workspace_id=ws,
        chunk_ids=["c-1"],
        document_ids=["d-1"],
    )
    c1 = GraphCommunity(
        id="c1",
        workspace_id=ws,
        title="Deleted acquisition",
        summary="Secret acquisition happened.",
        entity_ids=["missing-src"],
        relationship_ids=["r1"],
    )
    await store.upsert_relationships(ws, [r1])
    await store.upsert_communities(ws, [c1])

    removed = await store.tombstone_document(ws, "d-1")
    assert removed >= 1  # at least the relationship

    # Community must be tombstoned — stale summary must not surface.
    comms = await store.get_communities(ws)
    assert len(comms) == 0
    assert not any("Secret" in c.summary for c in comms)

    # Global search must not return the stale summary.
    retriever = GraphRetriever(store)
    gres = await retriever.global_search(ws, "Secret")
    assert gres.evidence_chunk_ids == []
    assert not any("Secret" in c.summary for c in gres.communities)
    store.close()


@pytest.mark.unit
async def test_sqlite_tombstone_all_entities_invalidates_community(
    tmp_path,
) -> None:
    """No-surviving-names fallback: community tombstoned when all entities deleted.

    Community over a single entity; deleting that entity leaves no survivable
    provenance, so the community must be tombstoned (not retained with stale
    title/summary).
    """
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-w"
    await store.ensure_graph(ws)
    e1 = Entity(
        id="e1", name="Alice", type=EntityType.PERSON, workspace_id=ws,
        chunk_ids=["c-1"], document_ids=["d-1"],
    )
    c1 = GraphCommunity(
        id="c1", workspace_id=ws, title="Alice Team",
        summary="Alice is the sole member.",
        entity_ids=["e1"], relationship_ids=[],
    )
    await store.upsert_entities(ws, [e1])
    await store.upsert_communities(ws, [c1])

    hit = await store.tombstone_entity(ws, "e1")
    assert hit is True

    # Community must be tombstoned — no stale summary survives.
    comms = await store.get_communities(ws)
    assert len(comms) == 0  # tombstoned, not returned by get_communities
    store.close()
    path = tmp_path / "g.db"
    ws = "ws-x"
    store = SQLiteGraphStore(path)
    await store.ensure_graph(ws)
    await store.upsert_entities(
        ws, [Entity(id="e1", name="Alice", type=EntityType.PERSON,
                     workspace_id=ws, document_ids=["d-1"])]
    )
    store.close()

    store2 = SQLiteGraphStore(path)
    await store2.ensure_graph(ws)  # idempotent schema; no errors
    got = await store2.get_entity(ws, "e1")
    assert got is not None and got.name == "Alice"
    store2.close()


@pytest.mark.unit
async def test_sqlite_context_manager_closes_and_lazy_reconnects(tmp_path) -> None:
    path = tmp_path / "g.db"
    ws = "ws-x"
    store = SQLiteGraphStore(path)
    with store:
        await store.ensure_graph(ws)
        await store.upsert_entities(
            ws, [Entity(id="e1", name="Alice", type=EntityType.PERSON,
                         workspace_id=ws, document_ids=["d-1"])]
        )
    assert store._conn is None  # deterministic close via __exit__

    # connection was closed; next op lazily reopens it
    got = await store.get_entity(ws, "e1")
    assert got is not None and got.name == "Alice"
    store.close()
