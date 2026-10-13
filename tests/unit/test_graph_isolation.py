# ruff: noqa: E501
"""T-V2-004: workspace isolation - cross-workspace reads rejected, disabled workspace empty."""

from __future__ import annotations

import pytest
from graph.extractor import GraphChunk, RuleBasedGraphExtractor
from graph.sqlite_store import SQLiteGraphStore


@pytest.mark.unit
async def test_cross_workspace_entity_leaks_zero(tmp_path) -> None:
    """Workspace B's retriever/hydrator cannot see workspace A's entities."""
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    # Enable both workspaces
    await store.set_enabled("ws-a", True)
    await store.set_enabled("ws-b", True)

    extractor = RuleBasedGraphExtractor()
    # Workspace A entity
    chunk_a = GraphChunk(chunk_id="ch-a", text="Alice Smith works at Acme Corp.", workspace_id="ws-a", document_id="doc-a")
    result_a = await extractor.extract([chunk_a])
    await store.upsert_extraction("ws-a", result_a.entities, result_a.relationships, result_a.communities)

    # Workspace B entity (different name)
    chunk_b = GraphChunk(chunk_id="ch-b", text="Bob Jones is located in Paris.", workspace_id="ws-b", document_id="doc-b")
    result_b = await extractor.extract([chunk_b])
    await store.upsert_extraction("ws-b", result_b.entities, result_b.relationships, result_b.communities)

    # Workspace B reads should not see A's entities
    # Try to get A's entity via B's workspace
    ent_a_id = result_a.entities[0].id if result_a.entities else None
    if ent_a_id:
        leaked = await store.get_entity("ws-b", ent_a_id)
        assert leaked is None, "cross-workspace leak: B saw A's entity"

    # B's find should not return A's names
    found_b = await store.find_entities_by_name("ws-b", "Alice Smith")
    assert found_b == [], "B's find returned A's entity"

    found_a = await store.find_entities_by_name("ws-a", "Alice Smith")
    assert len(found_a) > 0, "A should see its own entity"

    # Also check get_neighbors isolation
    if result_a.entities:
        neighbors_b, _ = await store.get_neighbors("ws-b", result_a.entities[0].id)
        assert neighbors_b == [], "B's get_neighbors leaked A's entity"

    # Count total leak
    leaks = 0
    # Try to fetch all of A's entities via B
    for ent in result_a.entities:
        if await store.get_entity("ws-b", ent.id) is not None:
            leaks += 1
    assert leaks == 0, f"cross-workspace entity leaks: {leaks}"
    store.close()


@pytest.mark.unit
async def test_disabled_workspace_returns_empty_never_errors(tmp_path) -> None:
    """Disabled workspace gets empty graph results, never errors, never other tenants' rows."""
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    # Workspace ws-enabled has data, ws-disabled explicitly disabled
    await store.set_enabled("ws-enabled", True)
    await store.set_enabled("ws-disabled", False)
    await store.set_enabled("ws-explicit-off", False)

    extractor = RuleBasedGraphExtractor()
    chunk = GraphChunk(chunk_id="ch-1", text="Alice Smith works at Acme Corp.", workspace_id="ws-enabled", document_id="doc-1")
    result = await extractor.extract([chunk])
    await store.upsert_extraction("ws-enabled", result.entities, result.relationships, result.communities)

    # Disabled workspace reads should be empty, not error
    assert await store.is_enabled("ws-disabled") is False
    assert await store.is_enabled("ws-explicit-off") is False
    assert await store.is_enabled("ws-enabled") is True

    # All read paths for disabled should return empty, never error
    assert await store.get_entity("ws-disabled", "any-id") is None
    assert await store.find_entities_by_name("ws-disabled", "Alice") == []
    n, r = await store.get_neighbors("ws-disabled", "any-id")
    assert n == [] and r == []
    assert await store.get_communities("ws-disabled") == []

    # Disabled workspace should not see enabled workspace's rows
    if result.entities:
        leaked = await store.get_entity("ws-disabled", result.entities[0].id)
        assert leaked is None
        leaked2 = await store.find_entities_by_name("ws-disabled", "Alice Smith")
        assert leaked2 == []

    # Upsert to disabled workspace should be no-op (pay nothing)
    chunk_disabled = GraphChunk(chunk_id="ch-dis", text="Charlie Brown works at Globex.", workspace_id="ws-disabled", document_id="doc-2")
    result_dis = await extractor.extract([chunk_disabled])
    n = await store.upsert_entities("ws-disabled", result_dis.entities)
    assert n == 0, "disabled workspace upsert should pay nothing"
    # And still empty on read
    assert await store.find_entities_by_name("ws-disabled", "Charlie") == []

    store.close()


@pytest.mark.unit
async def test_enabled_workspace_sees_own_data(tmp_path) -> None:
    """Enabled workspace sees its own data after enablement."""
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    await store.set_enabled("ws-1", True)
    extractor = RuleBasedGraphExtractor()
    chunk = GraphChunk(chunk_id="ch-1", text="Alice Smith works at Acme Corp.", workspace_id="ws-1", document_id="doc-1")
    result = await extractor.extract([chunk])
    await store.upsert_extraction("ws-1", result.entities, result.relationships, result.communities)
    # Should see own data
    assert len(result.entities) > 0
    fetched = await store.get_entity("ws-1", result.entities[0].id)
    assert fetched is not None
    assert fetched.name == result.entities[0].name
    store.close()
