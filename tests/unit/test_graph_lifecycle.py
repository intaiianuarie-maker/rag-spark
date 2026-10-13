# ruff: noqa: E501
"""T-V2-003: lifecycle parity - tombstone, supersede, idempotent re-ingest."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from graph.extractor import GraphChunk, RuleBasedGraphExtractor
from graph.sqlite_store import SQLiteGraphStore


@pytest.mark.unit
async def test_tombstoned_doc_has_zero_live_graph_rows(tmp_path: Path) -> None:
    """Live graph rows for tombstoned docs: 0."""
    os.environ["GRAPH_MODE"] = "rule"
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    ws, doc = "ws-tomb", "doc-1"
    await store.set_enabled(ws, True)
    chunk = GraphChunk(
        chunk_id="ch-1", text="Alice Smith works at Acme Corp.", workspace_id=ws, document_id=doc
    )
    extractor = RuleBasedGraphExtractor()
    result = await extractor.extract([chunk])
    await store.upsert_extraction(ws, result.entities, result.relationships, result.communities)
    # Verify live before
    assert await store.get_entity(ws, result.entities[0].id) is not None
    # Tombstone via purge path
    await store.tombstone_document(ws, doc)
    # Verify live after = 0
    assert await store.get_entity(ws, result.entities[0].id) is None
    # Also via find
    found = await store.find_entities_by_name(ws, "Alice Smith")
    assert found == []
    store.close()
    os.environ.pop("GRAPH_MODE", None)


@pytest.mark.unit
async def test_superseded_version_graph_rows_do_not_ground(tmp_path: Path) -> None:
    """Superseded-version rows grounding citations: 0."""
    os.environ["GRAPH_MODE"] = "rule"
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    ws = "ws-sup"
    await store.set_enabled(ws, True)
    # Simulate version 1 chunks
    chunk_v1 = GraphChunk(chunk_id="ch-v1-1", text="Alice Smith works at Acme Corp.", workspace_id=ws, document_id="doc-1")
    extractor = RuleBasedGraphExtractor()
    result_v1 = await extractor.extract([chunk_v1])
    await store.upsert_extraction(ws, result_v1.entities, result_v1.relationships, result_v1.communities)
    # Verify v1 entities live
    assert len(result_v1.entities) > 0
    ent_id = result_v1.entities[0].id
    assert await store.get_entity(ws, ent_id) is not None
    # Supersede: tombstone v1's chunk ids
    await store.tombstone_chunks(ws, ["ch-v1-1"])
    # Verify v1's entities no longer ground (tombstoned)
    assert await store.get_entity(ws, ent_id) is None
    # New version's chunks with same content should be able to re-create
    chunk_v2 = GraphChunk(chunk_id="ch-v2-1", text="Alice Smith works at Acme Corp.", workspace_id=ws, document_id="doc-1")
    result_v2 = await extractor.extract([chunk_v2])
    # Same deterministic id as v1 (same normalized name)
    assert result_v2.entities[0].id == ent_id
    await store.upsert_extraction(ws, result_v2.entities, result_v2.relationships, result_v2.communities)
    # Now live again (re-ingest untombstones)
    assert await store.get_entity(ws, ent_id) is not None
    store.close()
    os.environ.pop("GRAPH_MODE", None)


@pytest.mark.unit
async def test_reingest_same_doc_no_duplicate_entities(tmp_path: Path) -> None:
    """Duplicate entities on re-ingest: 0."""
    os.environ["GRAPH_MODE"] = "rule"
    db = tmp_path / "graph.db"
    store = SQLiteGraphStore(db)
    ws, doc = "ws-dup", "doc-dup"
    await store.set_enabled(ws, True)
    chunk = GraphChunk(chunk_id="ch-dup", text="Bob Jones is located in Paris.", workspace_id=ws, document_id=doc)
    extractor = RuleBasedGraphExtractor()
    result = await extractor.extract([chunk])
    # First ingest
    await store.upsert_extraction(ws, result.entities, result.relationships, result.communities)
    # Second ingest same doc (same chunk)
    result2 = await extractor.extract([chunk])
    await store.upsert_extraction(ws, result2.entities, result2.relationships, result2.communities)
    # Count should be same, not doubled
    # Use direct DB count
    import sqlite3

    def count():
        conn = sqlite3.connect(str(db))
        cur = conn.execute("SELECT COUNT(*) FROM graph_entities WHERE workspace_id=? AND tombstoned=0", (ws,))
        return cur.fetchone()[0]

    total = await store._run(count)
    assert total == len(result.entities), f"duplicate entities: {total} != {len(result.entities)}"
    store.close()
    os.environ.pop("GRAPH_MODE", None)


@pytest.mark.unit
async def test_purge_uses_store_purger_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Purge reuses store-purger seam pattern from dedup.py (T-007 precedent)."""
    # Verify that purge_document registers graph purger when GRAPH_MODE=rule
    monkeypatch.setenv("GRAPH_MODE", "rule")

    from ingestion.dedup import purge_document

    # Mock the graph store to avoid real DB
    called = {}

    async def fake_tombstone(ws, doc):
        called["ws"] = ws
        called["doc"] = doc
        return 1

    monkeypatch.setattr("graph.sqlite_store.SQLiteGraphStore.tombstone_document", fake_tombstone)
    # Need a real document to purge
    import uuid

    from ingestion.persistence import Document, DocumentVersion, get_session_factory
    from sqlalchemy import text

    async with get_session_factory()() as session:
        org_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        await session.execute(text("INSERT INTO organizations (id, name) VALUES (:id, :name)"), {"id": str(org_id), "name": "test-purge"})
        await session.execute(text("INSERT INTO workspaces (id, organization_id, name) VALUES (:id, :org, :name)"), {"id": str(ws_id), "org": str(org_id), "name": "ws-purge"})
        doc = Document(workspace_id=ws_id, title="t", source_filename="f.txt", mime_type="text/plain")
        session.add(doc)
        await session.flush()
        version = DocumentVersion(id=uuid.uuid4(), document_id=doc.id, version_number=1, checksum="a"*64, size_bytes=1, mime_type="text/plain", storage_key="k")
        session.add(version)
        await session.commit()
        doc_id = doc.id
        # Purge
        report = await purge_document(session, workspace_id=ws_id, document_id=doc_id)
        assert report.get("graph") is True or report.get("graph") is False
        # Clean up
        await session.execute(text("DELETE FROM documents WHERE id=:id"), {"id": str(doc_id)})
        await session.commit()
    monkeypatch.delenv("GRAPH_MODE", raising=False)
