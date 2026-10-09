"""Unit tests for Phase 10 Knowledge Graph, Store, Resolver, and Retriever."""

import pytest
from graph import (
    Entity,
    EntityType,
    GraphCommunity,
    GraphRetriever,
    MemoryGraphStore,
    Relationship,
    RelationshipType,
    SimpleEntityResolver,
)


@pytest.mark.unit
async def test_memory_graph_store_crud_and_isolation() -> None:
    store = MemoryGraphStore()
    ws1 = "ws-alpha"
    ws2 = "ws-beta"

    e1 = Entity(
        id="ent-1",
        name="Alice",
        type=EntityType.PERSON,
        workspace_id=ws1,
        chunk_ids=["chunk-101"],
    )
    e2 = Entity(
        id="ent-2",
        name="Acme Corp",
        type=EntityType.ORGANIZATION,
        workspace_id=ws1,
        chunk_ids=["chunk-102"],
    )
    r1 = Relationship(
        id="rel-1",
        source_entity_id="ent-1",
        target_entity_id="ent-2",
        type=RelationshipType.WORKS_FOR,
        workspace_id=ws1,
        chunk_ids=["chunk-101"],
    )

    # Upsert into ws1
    assert await store.upsert_entities(ws1, [e1, e2]) == 2
    assert await store.upsert_relationships(ws1, [r1]) == 1

    # Fetch in ws1
    fetched_e1 = await store.get_entity(ws1, "ent-1")
    assert fetched_e1 is not None
    assert fetched_e1.name == "Alice"

    # Workspace boundary isolation (R-004)
    assert await store.get_entity(ws2, "ent-1") is None
    assert await store.find_entities_by_name(ws2, "Alice") == []
    neighbors_ws2, rels_ws2 = await store.get_neighbors(ws2, "ent-1")
    assert neighbors_ws2 == []
    assert rels_ws2 == []


@pytest.mark.unit
async def test_subgraph_and_neighbor_traversal() -> None:
    store = MemoryGraphStore()
    ws = "ws-gamma"

    e1 = Entity(id="1", name="Alice", type=EntityType.PERSON, workspace_id=ws)
    e2 = Entity(id="2", name="TechLab", type=EntityType.ORGANIZATION, workspace_id=ws)
    e3 = Entity(id="3", name="Project X", type=EntityType.PROJECT, workspace_id=ws)

    r1 = Relationship(
        id="r1",
        source_entity_id="1",
        target_entity_id="2",
        type=RelationshipType.WORKS_FOR,
        workspace_id=ws,
    )
    r2 = Relationship(
        id="r2",
        source_entity_id="2",
        target_entity_id="3",
        type=RelationshipType.OWNS,
        workspace_id=ws,
    )

    await store.upsert_entities(ws, [e1, e2, e3])
    await store.upsert_relationships(ws, [r1, r2])

    # 1-hop traversal from Alice
    ents_1hop, rels_1hop = await store.get_neighbors(ws, "1", depth=1)
    assert {e.id for e in ents_1hop} == {"1", "2"}
    assert {r.id for r in rels_1hop} == {"r1"}

    # 2-hop traversal from Alice -> TechLab -> Project X
    ents_2hop, rels_2hop = await store.get_neighbors(ws, "1", depth=2)
    assert {e.id for e in ents_2hop} == {"1", "2", "3"}
    assert {r.id for r in rels_2hop} == {"r1", "r2"}

    # Filter by relationship type
    ents_filtered, rels_filtered = await store.get_neighbors(
        ws, "1", depth=2, relationship_types=[RelationshipType.WORKS_FOR]
    )
    assert {e.id for e in ents_filtered} == {"1", "2"}
    assert {r.id for r in rels_filtered} == {"r1"}


@pytest.mark.unit
def test_simple_entity_resolver() -> None:
    resolver = SimpleEntityResolver(manual_aliases={"msft": "microsoft"})
    ws = "ws-delta"

    e1 = Entity(
        id="1",
        name="Microsoft Corporation",
        type=EntityType.ORGANIZATION,
        workspace_id=ws,
        chunk_ids=["chunk-1"],
    )
    e2 = Entity(
        id="2",
        name="Microsoft Corp",
        type=EntityType.ORGANIZATION,
        workspace_id=ws,
        chunk_ids=["chunk-2"],
    )
    e3 = Entity(
        id="3",
        name="MSFT",
        type=EntityType.ORGANIZATION,
        workspace_id=ws,
        chunk_ids=["chunk-3"],
    )
    e4 = Entity(
        id="4",
        name="Apple Inc",
        type=EntityType.ORGANIZATION,
        workspace_id=ws,
        chunk_ids=["chunk-4"],
    )

    resolved = resolver.resolve_entities([e1, e2, e3, e4])
    assert len(resolved) == 2

    msft_entity = next(e for e in resolved if "Microsoft" in e.name)
    assert msft_entity.name == "Microsoft Corporation"
    assert "chunk-1" in msft_entity.chunk_ids
    assert "chunk-2" in msft_entity.chunk_ids
    assert "chunk-3" in msft_entity.chunk_ids
    assert "Microsoft Corp" in msft_entity.aliases or "MSFT" in msft_entity.aliases


@pytest.mark.unit
async def test_graph_retriever_local_and_global_search() -> None:
    store = MemoryGraphStore()
    retriever = GraphRetriever(store)
    ws = "ws-omega"

    # Setup entities
    e1 = Entity(
        id="e1",
        name="Dr. Smith",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["ch-1"],
    )
    e2 = Entity(
        id="e2",
        name="BioCorp",
        type=EntityType.ORGANIZATION,
        workspace_id=ws,
        chunk_ids=["ch-2"],
    )
    r1 = Relationship(
        id="rel1",
        source_entity_id="e1",
        target_entity_id="e2",
        type=RelationshipType.WORKS_FOR,
        description="Head of Genetics",
        workspace_id=ws,
        chunk_ids=["ch-1", "ch-2"],
    )
    await store.upsert_entities(ws, [e1, e2])
    await store.upsert_relationships(ws, [r1])

    # Setup community
    c1 = GraphCommunity(
        id="c1",
        workspace_id=ws,
        title="Genetics Department Overview",
        summary="BioCorp's genetics team led by Dr. Smith specializes in mRNA therapies.",
        entity_ids=["e1", "e2"],
    )
    await store.upsert_communities(ws, [c1])

    # Local search
    res_local = await retriever.local_search(ws, query="Where does Dr. Smith work?")
    assert len(res_local.entities) >= 1
    assert "ch-1" in res_local.evidence_chunk_ids
    assert "ch-2" in res_local.evidence_chunk_ids

    items = retriever.to_retrieved_items(res_local, ws)
    assert len(items) == 2
    assert all(it.metadata.get("is_graph") is True for it in items)
    assert {it.chunk_id for it in items} == {"ch-1", "ch-2"}

    # Global search
    res_global = await retriever.global_search(ws, query="genetics therapies")
    assert len(res_global.communities) == 1
    assert res_global.communities[0].title == "Genetics Department Overview"
    assert "ch-1" in res_global.evidence_chunk_ids
