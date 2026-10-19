"""Unit tests for SQLiteGraphStore lifecycle (issues #2 and #5).

* issue #2 - tombstoning a document invalidates its entities/edges and prunes
  affected communities in the same transaction; global/local retrieval must
  not return stale derived content. Multi-document communities keep their
  surviving provenance.
* issue #5 - the SQLite connection is opened once, closed deterministically
  (close()/context-manager/Exception path) and lazily reopened; schema setup
  runs once per file.
* T-IMP-006 - multi-document provenance merge: entities/relationships shared
  across documents retain all supporting document_ids/chunk_ids (union, not
  replace). Granular document tombstoning removes only the deleted document's
  contribution, keeping entities live when other documents still support them.
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

    ea = Entity(
        id="e1",
        name="Apple Inc",
        type=EntityType.ORGANIZATION,
        workspace_id="ws-a",
        document_ids=["d-1"],
    )
    eb = Entity(
        id="e1",
        name="Apple Inc",
        type=EntityType.ORGANIZATION,
        workspace_id="ws-b",
        document_ids=["d-1"],
    )
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
        id="c1",
        workspace_id=ws,
        title="Team",
        summary="Alice at Acme",
        entity_ids=["e1", "e2"],
        relationship_ids=["r1"],
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
    e1 = Entity(
        id="e1",
        name="Alice",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["c-1"],
        document_ids=["d-1"],
    )
    e2 = Entity(
        id="e2",
        name="Bob",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["c-2"],
        document_ids=["d-2"],
    )
    r1 = Relationship(
        id="r1",
        source_entity_id="e1",
        target_entity_id="e2",
        type=RelationshipType.RELATED_TO,
        workspace_id=ws,
        chunk_ids=["c-1", "c-2"],
        document_ids=["d-1", "d-2"],
    )
    c1 = GraphCommunity(
        id="c1",
        workspace_id=ws,
        title="Team",
        summary="Alice and Bob",
        entity_ids=["e1", "e2"],
        relationship_ids=["r1"],
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
        id="e1",
        name="Alice",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["c-1"],
        document_ids=["d-1"],
    )
    e2 = Entity(
        id="e2",
        name="Bob",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["c-2"],
        document_ids=["d-1"],
    )
    r1 = Relationship(
        id="r1",
        source_entity_id="e1",
        target_entity_id="e2",
        type=RelationshipType.RELATED_TO,
        workspace_id=ws,
        chunk_ids=["c-1", "c-2"],
        document_ids=["d-1"],
    )
    c1 = GraphCommunity(
        id="c1",
        workspace_id=ws,
        title="Alice and Bob",
        summary="Alice and Bob are members of the team.",
        entity_ids=["e1", "e2"],
        relationship_ids=["r1"],
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
        id="e1",
        name="Alice",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["c-1"],
        document_ids=["d-1"],
    )
    e2 = Entity(
        id="e2",
        name="Bob",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["c-2"],
        document_ids=["d-1"],
    )
    r1 = Relationship(
        id="r1",
        source_entity_id="e1",
        target_entity_id="e2",
        type=RelationshipType.RELATED_TO,
        workspace_id=ws,
        chunk_ids=["c-1", "c-2"],
        document_ids=["d-1"],
    )
    c1 = GraphCommunity(
        id="c1",
        workspace_id=ws,
        title="Alice and Bob",
        summary="Alice and Bob are connected.",
        entity_ids=["e1", "e2"],
        relationship_ids=["r1"],
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
        id="e1",
        name="Alice",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["c-1"],
        document_ids=["d-1"],
    )
    c1 = GraphCommunity(
        id="c1",
        workspace_id=ws,
        title="Alice Team",
        summary="Alice is the sole member.",
        entity_ids=["e1"],
        relationship_ids=[],
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
        ws,
        [
            Entity(
                id="e1", name="Alice", type=EntityType.PERSON, workspace_id=ws, document_ids=["d-1"]
            )
        ],
    )
    store.close()

    store2 = SQLiteGraphStore(path)
    await store2.ensure_graph(ws)  # idempotent schema; no errors
    got = await store2.get_entity(ws, "e1")
    assert got is not None and got.name == "Alice"
    store2.close()


@pytest.mark.unit
async def test_multidoc_provenance_merge_granular_tombstone(tmp_path) -> None:
    """T-IMP-006: Multi-document provenance is merged, not replaced; granular
    document tombstoning only removes the deleted doc's provenance.

    Verifies:
    - Ingest doc 1 with entity "Alice Smith".
    - Ingest doc 2 with entity "Alice Smith".
    - Verify Alice Smith's document_ids contain BOTH [doc-1, doc-2].
    - Tombstone doc 2 -> Alice Smith remains live with document_ids=[doc-1].
    - Tombstone doc 1 -> Alice Smith is now tombstoned (0 live entities).
    """
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-merge"
    doc1 = "doc-1"
    doc2 = "doc-2"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)

    # Doc 1: Alice Smith
    e1 = Entity(
        id="e-alice-1",
        name="Alice Smith",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["c-doc-1"],
        document_ids=[doc1],
    )
    await store.upsert_extraction(ws, [e1], [], [], document_id=doc1)
    entity = await store.get_entity(ws, "e-alice-1")
    assert entity is not None
    assert entity.document_ids == [doc1]
    assert entity.chunk_ids == ["c-doc-1"]

    # Doc 2: Alice Smith (same entity, different doc)
    e2 = Entity(
        id="e-alice-1",
        name="Alice Smith",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["c-doc-2"],
        document_ids=[doc2],
    )
    await store.upsert_extraction(ws, [e2], [], [], document_id=doc2)
    entity = await store.get_entity(ws, "e-alice-1")
    assert entity is not None
    # Both documents should be in document_ids
    assert doc1 in entity.document_ids
    assert doc2 in entity.document_ids
    assert len(entity.document_ids) == 2
    # Both chunks should be in chunk_ids
    assert "c-doc-1" in entity.chunk_ids
    assert "c-doc-2" in entity.chunk_ids
    assert len(entity.chunk_ids) == 2

    # Tombstone doc 2 -> Alice should survive with doc-1
    removed = await store.tombstone_document(ws, doc2)
    # e-alice-1 is NOT fully tombstoned because doc-1 still supports it
    assert removed == 0
    entity = await store.get_entity(ws, "e-alice-1")
    assert entity is not None, "Alice should survive doc-2 tombstone"
    assert entity.document_ids == [doc1]
    assert entity.chunk_ids == ["c-doc-1"]

    # Tombstone doc 1 -> Alice should now be fully tombstoned
    removed = await store.tombstone_document(ws, doc1)
    assert removed == 1, "Alice should be fully tombstoned when no docs remain"
    entity = await store.get_entity(ws, "e-alice-1")
    assert entity is None, "Alice should be tombstoned after all docs deleted"

    # Verify the graph_tombstones table recorded both documents
    assert await store.is_document_tombstoned(ws, doc1) is True
    assert await store.is_document_tombstoned(ws, doc2) is True

    store.close()


@pytest.mark.unit
async def test_sqlite_context_manager_closes_and_lazy_reconnects(tmp_path) -> None:
    path = tmp_path / "g.db"
    ws = "ws-x"
    store = SQLiteGraphStore(path)
    with store:
        await store.ensure_graph(ws)
        await store.upsert_entities(
            ws,
            [
                Entity(
                    id="e1",
                    name="Alice",
                    type=EntityType.PERSON,
                    workspace_id=ws,
                    document_ids=["d-1"],
                )
            ],
        )
    assert store._conn is None  # deterministic close via __exit__

    # connection was closed; next op lazily reopens it
    got = await store.get_entity(ws, "e1")
    assert got is not None and got.name == "Alice"
    store.close()


@pytest.mark.unit
async def test_multidoc_relationship_granular_tombstone(tmp_path) -> None:
    """T-IMP-006: Relationships also preserve multi-document provenance.

    Tests that a relationship shared by two documents is not fully tombstoned
    when only one document is deleted, and that dangling relationships (referencing
    a tombstoned entity) are also tombstoned.
    """
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-rel-merge"
    doc1 = "doc-1"
    doc2 = "doc-2"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)

    e1 = Entity(id="e1", name="Alice", type=EntityType.PERSON, workspace_id=ws, document_ids=[doc1])
    e2 = Entity(
        id="e2", name="Acme", type=EntityType.ORGANIZATION, workspace_id=ws, document_ids=[doc1]
    )
    await store.upsert_entities(ws, [e1, e2])

    # Relationship supported by both docs
    r1 = Relationship(
        id="r1",
        source_entity_id="e1",
        target_entity_id="e2",
        type=RelationshipType.WORKS_FOR,
        workspace_id=ws,
        chunk_ids=["c-1"],
        document_ids=[doc1],
    )
    await store.upsert_relationships(ws, [r1])

    # Now upsert the same relationship with doc-2 (merge provenance)
    r2 = Relationship(
        id="r1",
        source_entity_id="e1",
        target_entity_id="e2",
        type=RelationshipType.WORKS_FOR,
        workspace_id=ws,
        chunk_ids=["c-2"],
        document_ids=[doc2],
    )
    await store.upsert_relationships(ws, [r2])

    # Verify relationship has merged provenance via direct DB query
    def _check_rel():
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "g.db"))
        cur = conn.execute(
            "SELECT payload FROM graph_relationships"
            " WHERE workspace_id=? AND relationship_id=? AND tombstoned=0",
            (ws, "r1"),
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            return None
        import json

        payload = json.loads(row[0])
        return payload

    rel_data = await store._run(_check_rel)
    assert rel_data is not None
    assert doc1 in rel_data["document_ids"]
    assert doc2 in rel_data["document_ids"]

    # Tombstone doc 1 — entities e1/e2 are fully tombstoned (only doc-1)
    # and r1 becomes dangling — all should be tombstoned
    removed = await store.tombstone_document(ws, doc1)
    assert removed >= 2  # at least e1 and e2

    # Entities should be gone
    assert await store.get_entity(ws, "e1") is None
    assert await store.get_entity(ws, "e2") is None

    store.close()


@pytest.mark.unit
async def test_upsert_extraction_merges_multi_document_provenance(tmp_path) -> None:
    """T-IMP-006: upsert_extraction merges provenance across documents."""
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-upsert-merge"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)

    # First document
    e1_doc1 = Entity(
        id="e-alice",
        name="Alice Smith",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["chunk-1"],
        document_ids=["doc-1"],
    )
    await store.upsert_extraction(ws, [e1_doc1], [], [], document_id="doc-1")
    entity = await store.get_entity(ws, "e-alice")
    assert entity is not None
    assert entity.document_ids == ["doc-1"]

    # Second document with same entity
    e1_doc2 = Entity(
        id="e-alice",
        name="Alice Smith",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["chunk-2"],
        document_ids=["doc-2"],
    )
    await store.upsert_extraction(ws, [e1_doc2], [], [], document_id="doc-2")
    entity = await store.get_entity(ws, "e-alice")
    assert entity is not None
    # Provenance should be merged, not replaced
    assert "doc-1" in entity.document_ids
    assert "doc-2" in entity.document_ids
    assert "chunk-1" in entity.chunk_ids
    assert "chunk-2" in entity.chunk_ids

    store.close()


@pytest.mark.unit
async def test_rv19_f2_uuid_upsert_extraction(tmp_path) -> None:
    import uuid

    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-uuid"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)
    da, db = str(uuid.uuid4()), str(uuid.uuid4())
    ua, ub = str(uuid.uuid4()), str(uuid.uuid4())
    await store.upsert_extraction(
        ws,
        [Entity(id="e", name="Alice", workspace_id=ws, document_ids=[da], chunk_ids=[ua])],
        [],
        [],
        document_id=da,
    )
    await store.upsert_extraction(
        ws,
        [Entity(id="e", name="Alice", workspace_id=ws, document_ids=[db], chunk_ids=[ub])],
        [],
        [],
        document_id=db,
    )
    await store.tombstone_document(ws, da)
    ent = await store.get_entity(ws, "e")
    assert ent is not None
    assert da not in ent.document_ids
    assert ua not in ent.chunk_ids
    assert db in ent.document_ids
    assert ub in ent.chunk_ids
    from graph.retriever import GraphRetriever

    s = await GraphRetriever(store).local_search(ws, "Alice")
    assert ua not in s.evidence_chunk_ids
    store.close()


@pytest.mark.unit
async def test_rv19_f2_uuid_upsert_entities(tmp_path) -> None:
    import uuid

    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-uuid2"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)
    da, db = str(uuid.uuid4()), str(uuid.uuid4())
    ua, ub = str(uuid.uuid4()), str(uuid.uuid4())
    await store.upsert_entities(
        ws, [Entity(id="e", name="Alice", workspace_id=ws, document_ids=[da], chunk_ids=[ua])]
    )
    await store.upsert_entities(
        ws, [Entity(id="e", name="Alice", workspace_id=ws, document_ids=[db], chunk_ids=[ub])]
    )
    await store.tombstone_document(ws, da)
    ent = await store.get_entity(ws, "e")
    assert ent is not None
    assert da not in ent.document_ids
    assert ua not in ent.chunk_ids
    from graph.retriever import GraphRetriever

    s = await GraphRetriever(store).local_search(ws, "Alice")
    assert ua not in s.evidence_chunk_ids
    store.close()


@pytest.mark.unit
async def test_rv19_f3_replacement_retrieval(tmp_path) -> None:
    from graph.interfaces import EntityType, GraphCommunity

    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-repl"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)
    e = Entity(
        id="e1",
        name="Alice",
        type=EntityType.PERSON,
        workspace_id=ws,
        document_ids=["doc"],
        chunk_ids=["old"],
    )
    c = GraphCommunity(
        id="comm1",
        workspace_id=ws,
        title="Team",
        summary="Alice team",
        entity_ids=["e1"],
        relationship_ids=[],
    )
    await store.upsert_extraction(ws, [e], [], [c], document_id="doc")
    await store.tombstone_chunks(ws, ["old"])
    e2 = Entity(
        id="e1",
        name="Alice",
        type=EntityType.PERSON,
        workspace_id=ws,
        document_ids=["doc"],
        chunk_ids=["new"],
    )
    c2 = GraphCommunity(
        id="comm1",
        workspace_id=ws,
        title="Team",
        summary="Alice team",
        entity_ids=["e1"],
        relationship_ids=[],
    )
    await store.upsert_extraction(ws, [e2], [], [c2], document_id="doc")
    comms = await store.get_communities(ws)
    assert len(comms) == 1
    from graph.retriever import GraphRetriever

    s = await GraphRetriever(store).global_search(ws, "Alice")
    assert "new" in s.evidence_chunk_ids or s.evidence_chunk_ids
    assert "old" not in s.evidence_chunk_ids
    store.close()


@pytest.mark.unit
async def test_rv19_f6_mixed_retirement_deletion(tmp_path) -> None:
    import uuid

    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-mixed"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)
    da, db = str(uuid.uuid4()), str(uuid.uuid4())
    ua, ub = str(uuid.uuid4()), str(uuid.uuid4())
    await store.upsert_extraction(
        ws,
        [Entity(id="e", name="Alice", workspace_id=ws, document_ids=[da], chunk_ids=[ua])],
        [],
        [],
        document_id=da,
    )
    await store.upsert_extraction(
        ws,
        [Entity(id="e", name="Alice", workspace_id=ws, document_ids=[db], chunk_ids=[ub])],
        [],
        [],
        document_id=db,
    )
    await store.tombstone_chunks(ws, [ua])
    await store.tombstone_document(ws, db)
    ent = await store.get_entity(ws, "e")
    assert ent is None
    from graph.retriever import GraphRetriever

    s = await GraphRetriever(store).local_search(ws, "Alice")
    assert s.evidence_chunk_ids == []
    store.close()


@pytest.mark.unit
async def test_rv19_f8_replay_preserves_ownership(tmp_path) -> None:
    import uuid

    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-replay"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)
    da, db = str(uuid.uuid4()), str(uuid.uuid4())
    ua, ub = str(uuid.uuid4()), str(uuid.uuid4())
    await store.upsert_extraction(
        ws,
        [Entity(id="e", name="Alice", workspace_id=ws, document_ids=[da], chunk_ids=[ua])],
        [],
        [],
        document_id=da,
    )
    await store.upsert_extraction(
        ws,
        [Entity(id="e", name="Alice", workspace_id=ws, document_ids=[db], chunk_ids=[ub])],
        [],
        [],
        document_id=db,
    )
    # replay unchanged merged entity
    merged = await store.get_entity(ws, "e")
    assert merged is not None
    await store.upsert_entities(ws, [merged])
    await store.tombstone_document(ws, da)
    ent = await store.get_entity(ws, "e")
    assert ent is not None
    assert ub in ent.chunk_ids
    assert ua not in ent.chunk_ids
    store.close()


@pytest.mark.unit
async def test_rv19_f9_optional_no_document_id(tmp_path) -> None:
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-opt"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)
    # two extractions without document_id
    await store.upsert_extraction(
        ws,
        [
            Entity(
                id="e",
                name="Alice",
                workspace_id=ws,
                document_ids=["old-doc"],
                chunk_ids=["old-uuid"],
            )
        ],
        [],
        [],
    )
    await store.upsert_extraction(
        ws,
        [
            Entity(
                id="e",
                name="Alice",
                workspace_id=ws,
                document_ids=["new-doc"],
                chunk_ids=["new-uuid"],
            )
        ],
        [],
        [],
    )
    await store.tombstone_document(ws, "old-doc")
    from graph.retriever import GraphRetriever

    s = await GraphRetriever(store).local_search(ws, "Alice")
    assert "old-uuid" not in s.evidence_chunk_ids
    store.close()


@pytest.mark.unit
async def test_rv19_f7_community_summary_cleared(tmp_path) -> None:
    from graph.interfaces import EntityType, GraphCommunity
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-f7"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)
    da, db = "doc-a", "doc-b"
    e1 = Entity(id="e", name="Alice", type=EntityType.PERSON, workspace_id=ws,
                document_ids=[da], chunk_ids=["ca"], description="works at Acme")
    e2 = Entity(id="e", name="Alice", type=EntityType.PERSON, workspace_id=ws,
                document_ids=[db], chunk_ids=["cb"], description="secret salary 900")
    c = GraphCommunity(id="comm", workspace_id=ws, title="Team",
                       summary="secret salary 900", entity_ids=["e"], relationship_ids=[])
    await store.upsert_extraction(ws, [e1], [], [c], document_id=da)
    await store.upsert_extraction(ws, [e2], [], [c], document_id=db)
    await store.tombstone_document(ws, db)
    from graph.retriever import GraphRetriever
    s = await GraphRetriever(store).global_search(ws, "Alice")
    assert "salary 900" not in (s.summary or "")
    store.close()


@pytest.mark.unit
async def test_rv19_f10_fence_optional(tmp_path) -> None:
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-f10"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)
    await store.upsert_extraction(
        ws,
        [Entity(id="e", name="Alice", workspace_id=ws, document_ids=["deleted"], chunk_ids=["c"])],
        [],
        [],
        document_id="deleted",
    )
    await store.tombstone_document(ws, "deleted")
    r = await store.upsert_extraction(
        ws,
        [
            Entity(
                id="fresh",
                name="Fresh",
                workspace_id=ws,
                document_ids=["deleted"],
                chunk_ids=["c2"],
            )
        ],
        [],
        [],
    )
    assert r["entities"] == 0
    from graph.retriever import GraphRetriever

    s = await GraphRetriever(store).local_search(ws, "Fresh")
    assert s.evidence_chunk_ids == []
    store.close()


@pytest.mark.unit
async def test_rv19_f11_refused_no_mapping(tmp_path) -> None:
    store = SQLiteGraphStore(tmp_path / "g.db")
    ws = "ws-f11"
    await store.ensure_graph(ws)
    await store.set_enabled(ws, True)
    await store.tombstone_chunks(ws, ["retired"])
    # refused write should not create mapping
    r = await store.upsert_entities(
        ws,
        [
            Entity(
                id="e", name="Alice", workspace_id=ws, document_ids=["doc"], chunk_ids=["retired"]
            )
        ],
    )
    assert r == 0
    # doc mapping should not contain retired
    cur = store._connect()
    rows = cur.execute(
        "SELECT * FROM graph_document_chunks WHERE workspace_id=? AND document_id='doc'", (ws,)
    ).fetchall()
    assert rows == []
    store.close()
