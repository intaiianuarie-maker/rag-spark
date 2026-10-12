"""Unit tests for graph workspace deletion (V2 work package G2 lifecycle).

`GraphStore.delete_workspace` is the hard-purge counterpart to the per-node
tombstone path: dropping a workspace must physically remove its entities,
relationships, and communities so no later retrieval (local or global) can
surface stale derived content, while other workspaces stay intact.

Covers both backends (in-memory reference + embedded SQLite default):

* purge returns the exact entities+relationships+communities count removed;
* reads, traversal, and community/global retrieval are empty afterwards;
* other workspaces are untouched (negative test);
* unknown workspace deletes to 0 without raising (fail-closed);
* the namespace is reusable after deletion;
* SQLite purge is durable across close/reopen (not just connection-local).
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
    SQLiteGraphStore,
)

_WS = "ws-gone"
_OTHER = "ws-stays"


def _seed_entities(ws: str) -> tuple[list[Entity], list[Relationship], list[GraphCommunity]]:
    alice = Entity(
        id="ent-alice",
        name="Alice Smith",
        type=EntityType.PERSON,
        workspace_id=ws,
        chunk_ids=["ch-1"],
        document_ids=["doc-1"],
    )
    acme = Entity(
        id="ent-acme",
        name="Acme Corporation",
        type=EntityType.ORGANIZATION,
        workspace_id=ws,
        chunk_ids=["ch-2"],
        document_ids=["doc-1"],
    )
    rel = Relationship(
        id="rel-works",
        source_entity_id="ent-alice",
        target_entity_id="ent-acme",
        type=RelationshipType.WORKS_FOR,
        workspace_id=ws,
        chunk_ids=["ch-1", "ch-2"],
        document_ids=["doc-1"],
    )
    comm = GraphCommunity(
        id="comm-1",
        workspace_id=ws,
        title="Alice + Acme",
        summary="Alice Smith works at Acme Corporation",
        entity_ids=["ent-alice", "ent-acme"],
        relationship_ids=["rel-works"],
    )
    return [alice, acme], [rel], [comm]


async def _seed(store: MemoryGraphStore | SQLiteGraphStore, ws: str) -> None:
    entities, rels, comms = _seed_entities(ws)
    await store.ensure_graph(ws)
    assert await store.upsert_entities(ws, entities) == 2
    assert await store.upsert_relationships(ws, rels) == 1
    assert await store.upsert_communities(ws, comms) == 1


async def _assert_purged(store: MemoryGraphStore | SQLiteGraphStore, ws: str) -> None:
    assert await store.get_entity(ws, "ent-alice") is None
    assert await store.find_entities_by_name(ws, "alice") == []
    ents, rels = await store.get_neighbors(ws, "ent-alice")
    assert ents == [] and rels == []
    assert await store.get_communities(ws) == []
    retriever = GraphRetriever(store)
    assert (await retriever.local_search(workspace_id=ws, query="Alice Smith")).entities == []
    assert (await retriever.global_search(workspace_id=ws, query="Acme")).communities == []


@pytest.mark.unit
async def test_memory_delete_workspace_purges_only_target_workspace() -> None:
    store = MemoryGraphStore()
    await _seed(store, _WS)
    await _seed(store, _OTHER)

    removed = await store.delete_workspace(_WS)
    assert removed == 4  # 2 entities + 1 relationship + 1 community
    await _assert_purged(store, _WS)

    # The surviving workspace keeps every row and still retrieves.
    survivor = await store.get_entity(_OTHER, "ent-alice")
    assert survivor is not None and survivor.workspace_id == _OTHER
    assert len(await store.find_entities_by_name(_OTHER, "alice")) == 1
    assert len(await store.get_communities(_OTHER)) == 1


@pytest.mark.unit
async def test_memory_delete_unknown_workspace_returns_zero() -> None:
    store = MemoryGraphStore()
    await _seed(store, _WS)
    assert await store.delete_workspace("ws-never-existed") == 0
    # Seeded data is untouched by the no-op delete.
    assert await store.get_entity(_WS, "ent-alice") is not None


@pytest.mark.unit
async def test_memory_workspace_reusable_after_delete() -> None:
    store = MemoryGraphStore()
    await _seed(store, _WS)
    assert await store.delete_workspace(_WS) == 4
    await _seed(store, _WS)
    got = await store.get_entity(_WS, "ent-acme")
    assert got is not None and got.name == "Acme Corporation"


@pytest.mark.unit
async def test_sqlite_delete_workspace_purges_only_target_workspace(tmp_path) -> None:
    store = SQLiteGraphStore(tmp_path / "g.db")
    try:
        await _seed(store, _WS)
        await _seed(store, _OTHER)

        removed = await store.delete_workspace(_WS)
        assert removed == 4  # 2 entities + 1 relationship + 1 community
        await _assert_purged(store, _WS)

        survivor = await store.get_entity(_OTHER, "ent-alice")
        assert survivor is not None and survivor.workspace_id == _OTHER
        assert len(await store.find_entities_by_name(_OTHER, "alice")) == 1
        assert len(await store.get_communities(_OTHER)) == 1
    finally:
        store.close()


@pytest.mark.unit
async def test_sqlite_delete_unknown_workspace_returns_zero(tmp_path) -> None:
    store = SQLiteGraphStore(tmp_path / "g.db")
    try:
        await _seed(store, _WS)
        assert await store.delete_workspace("ws-never-existed") == 0
        assert await store.get_entity(_WS, "ent-alice") is not None
    finally:
        store.close()


@pytest.mark.unit
async def test_sqlite_delete_is_durable_across_reopen(tmp_path) -> None:
    path = tmp_path / "g.db"
    store = SQLiteGraphStore(path)
    await _seed(store, _WS)
    assert await store.delete_workspace(_WS) == 4
    store.close()

    reopened = SQLiteGraphStore(path)
    try:
        await _assert_purged(reopened, _WS)
    finally:
        reopened.close()


@pytest.mark.unit
async def test_sqlite_workspace_reusable_after_delete(tmp_path) -> None:
    store = SQLiteGraphStore(tmp_path / "g.db")
    try:
        await _seed(store, _WS)
        assert await store.delete_workspace(_WS) == 4
        await _seed(store, _WS)
        got = await store.get_entity(_WS, "ent-acme")
        assert got is not None and got.name == "Acme Corporation"
    finally:
        store.close()
