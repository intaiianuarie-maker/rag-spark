"""Adversarial regressions (museR) for POLICE F7-community and F9.

dev2's equivalents (test_rv19_f7_community_summary_cleared, test_rv19_f9_optional_no_document_id)
PASS on a8414fd where the defects exist (no teeth), so museR authored these from its harness.
Each FAILS on a8414fd and passes on the fix.
"""

import uuid

from graph.interfaces import Entity, GraphCommunity, Relationship, RelationshipType
from graph.retriever import GraphRetriever
from graph.sqlite_store import SQLiteGraphStore


def _E(eid, doc, chunk, desc=""):
    return Entity(id=eid, name=eid, description=desc, workspace_id="ws",
                  document_ids=[doc], chunk_ids=[chunk])


async def test_rv19_f7_community_text_provenance(tmp_path):
    """Deleting a document must drop its text from the community title/summary, even when
    every member id survives."""
    s = SQLiteGraphStore(tmp_path / "f7.sqlite")
    await s.upsert_entities("ws", [_E("alice", "doc-a", "uuid-a", "works at Acme")])
    await s.upsert_entities("ws", [_E("alice", "doc-b", "uuid-b", "salary 900")])
    await s.upsert_communities("ws", [GraphCommunity(
        id="c1", workspace_id="ws", title="Alice", summary="Alice salary 900",
        entity_ids=["alice"])])
    await s.tombstone_document("ws", "doc-b")
    gs = await GraphRetriever(s).global_search("ws", "Alice")
    s.close()
    assert "salary 900" not in gs.summary, "deleted doc text survived in community summary (F7)"


async def test_rv19_f9_optional_writes_preserve_ownership(tmp_path):
    """Writes without document_id must keep unambiguous ownership: deleting doc-a must not
    over-delete the valid doc-b entity."""
    s = SQLiteGraphStore(tmp_path / "f9.sqlite")
    ua, ub = str(uuid.uuid4()), str(uuid.uuid4())
    await s.upsert_extraction("ws", [_E("alice", "doc-a", ua)], [], [], document_id=None)
    await s.upsert_extraction("ws", [_E("alice", "doc-b", ub)], [], [], document_id=None)
    await s.tombstone_document("ws", "doc-a")
    ent = await s.get_entity("ws", "alice")
    s.close()
    assert ent is not None, "valid doc-b entity was over-deleted (F9)"
    assert "doc-b" in ent.document_ids and ub in ent.chunk_ids, "surviving ownership lost (F9)"


async def test_rv19_f9_no_empty_live_row(tmp_path):
    """A delete must not leave a row alive with zero chunks (unsupported-but-searchable)."""
    s = SQLiteGraphStore(tmp_path / "f9b.sqlite")
    ua, ub = str(uuid.uuid4()), str(uuid.uuid4())
    await s.upsert_extraction("ws", [_E("alice", "doc-a", ua)], [], [], document_id=None)
    await s.upsert_extraction("ws", [_E("alice", "doc-b", ub)], [], [], document_id=None)
    await s.upsert_extraction("ws", [_E("bob", "doc-z", "uz")], [], [], document_id="doc-z")
    await s.tombstone_document("ws", "doc-a")
    ent = await s.get_entity("ws", "alice")
    s.close()
    assert not (ent is not None and not ent.chunk_ids), "empty live row left after delete (F9)"


async def test_rv19_f11_rel_refused_no_mapping(tmp_path):
    """A refused RELATIONSHIP write must not insert ownership mappings (F11 relationship path)."""
    s = SQLiteGraphStore(tmp_path / "f11rel.sqlite")
    await s.upsert_extraction("ws", [_E("e0", "doc-a", "uuid-a")], [], [], document_id="doc-a")
    await s.tombstone_chunks("ws", ["retired-x"])
    rel = Relationship(id="rel", source_entity_id="e0", target_entity_id="e0",
                       type=next(iter(RelationshipType)), workspace_id="ws",
                       document_ids=["doc-b"], chunk_ids=["retired-x", "uuid-a"])
    n = await s.upsert_relationships("ws", [rel])          # refused (carries a retired chunk)
    await s.tombstone_document("ws", "doc-b")
    refresh = await s.upsert_entities("ws", [_E("e0", "doc-a", "uuid-a")])
    s.close()
    assert n == 0, "refused relationship was written (F11-rel)"
    assert refresh == 1, "valid doc-a refresh blocked after doc-b delete (F11-rel)"


async def test_rv19_f12_unrelated_community_preserved(tmp_path):
    """A delete/retirement must not rewrite UNRELATED live community summaries (F12)."""
    s = SQLiteGraphStore(tmp_path / "f12.sqlite")
    await s.upsert_entities("ws", [_E("live", "live-doc", "live-chunk")])
    await s.upsert_communities("ws", [GraphCommunity(
        id="cm", workspace_id="ws", title="Merger negotiations",
        summary="Merger negotiations live-chunk", entity_ids=["live"])])
    await s.upsert_entities("ws", [_E("other", "unrelated-doc", "unrelated-chunk")])
    await s.tombstone_document("ws", "unrelated-doc")
    gs = await GraphRetriever(s).global_search("ws", "Merger")
    s.close()
    assert "Merger" in gs.summary, "unrelated delete overwrote a live community summary (F12)"
    assert "live-chunk" in gs.evidence_chunk_ids, "unrelated delete dropped live evidence (F12)"


async def test_rv19_f9_legacy_pre_mapping_schema(tmp_path):
    """A genuine pre-mapping schema must not leave an empty live/searchable row (F9 legacy)."""
    path = tmp_path / "f9legacy.sqlite"
    s = SQLiteGraphStore(path)
    conn = s._connect()
    conn.execute("DROP TABLE graph_document_chunks")       # simulate pre-mapping schema
    legacy = Entity(id="alice", name="alice", workspace_id="ws",
                    document_ids=["legacy-a", "legacy-b"],
                    chunk_ids=["legacy-uuid-a", "legacy-uuid-b"])
    conn.execute(
        "INSERT INTO graph_entities(workspace_id,entity_id,payload,tombstoned) VALUES(?,?,?,0)",
        ("ws", "alice", legacy.model_dump_json()))
    conn.commit()
    s.close()
    s2 = SQLiteGraphStore(path)
    await s2.upsert_extraction("ws", [_E("other", "unrelated-doc", "uz")], [], [],
                               document_id="unrelated-doc")
    await s2.tombstone_document("ws", "legacy-a")
    ent = await s2.get_entity("ws", "alice")
    s2.close()
    assert not (ent is not None and not ent.chunk_ids), "empty live row from legacy provenance (F9)"


async def test_rv19_f7_shared_retirement_rebuilds_community(tmp_path):
    """Retiring one document's chunk while the entity SURVIVES on another document must still
    regenerate the intersecting community (F7 regression in scoped rebuild)."""
    s = SQLiteGraphStore(tmp_path / "f7r.sqlite")
    await s.upsert_entities("ws", [_E("alice", "doc-a", "chunk-a", "works at Acme")])
    await s.upsert_entities("ws", [_E("alice", "doc-b", "chunk-b", "salary 900")])
    await s.upsert_communities("ws", [GraphCommunity(
        id="c1", workspace_id="ws", title="Alice", summary="Alice salary 900",
        entity_ids=["alice"])])
    await s.tombstone_chunks("ws", ["chunk-b"])
    gs = await GraphRetriever(s).global_search("ws", "Alice")
    s.close()
    assert "salary 900" not in gs.summary, "surviving entity did not rebuild community (F7)"


async def test_rv19_f9_overdeletion_keeps_verified_survivor(tmp_path):
    """Deleting an unmapped legacy document must not over-delete a survivor with verified
    ownership (F9 over-deletion)."""
    path = tmp_path / "f9od.sqlite"
    s = SQLiteGraphStore(path)
    conn = s._connect()
    conn.execute("DROP TABLE graph_document_chunks")       # pre-mapping schema
    legacy = Entity(id="alice", name="alice", workspace_id="ws",
                    document_ids=["old-doc"], chunk_ids=["old-uuid"])
    conn.execute(
        "INSERT INTO graph_entities(workspace_id,entity_id,payload,tombstoned) VALUES(?,?,?,0)",
        ("ws", "alice", legacy.model_dump_json()))
    conn.commit()
    s.close()
    s2 = SQLiteGraphStore(path)
    await s2.upsert_extraction("ws", [_E("alice", "new-doc", "new-uuid")], [], [],
                               document_id="new-doc")
    await s2.tombstone_document("ws", "old-doc")
    ent = await s2.get_entity("ws", "alice")
    s2.close()
    assert ent is not None, "verified surviving doc was over-deleted (F9)"
    assert "old-uuid" not in ent.chunk_ids, "deleted legacy uuid survived (F9)"
    assert "new-uuid" in ent.chunk_ids, "verified surviving uuid was dropped (F9)"


async def test_rv19_f9_partial_mapping_excludes_unverified(tmp_path):
    """A partially-mapped deleted document must not leave unverified legacy chunks behind,
    while the verified survivor is preserved (F9 partial mapping)."""
    path = tmp_path / "f9partial.sqlite"
    s = SQLiteGraphStore(path)
    conn = s._connect()
    conn.execute("DROP TABLE graph_document_chunks")       # pre-mapping row for old-doc
    legacy = Entity(id="alice", name="alice", workspace_id="ws",
                    document_ids=["old-doc"], chunk_ids=["legacy-old-uuid"])
    conn.execute(
        "INSERT INTO graph_entities(workspace_id,entity_id,payload,tombstoned) VALUES(?,?,?,0)",
        ("ws", "alice", legacy.model_dump_json()))
    conn.commit()
    s.close()
    s2 = SQLiteGraphStore(path)
    await s2.upsert_extraction("ws", [_E("alice", "old-doc", "old-mapped-uuid")], [], [],
                               document_id="old-doc")
    await s2.upsert_extraction("ws", [_E("alice", "new-doc", "new-uuid")], [], [],
                               document_id="new-doc")
    await s2.tombstone_document("ws", "old-doc")
    s2.close()
    s3 = SQLiteGraphStore(path)
    search = await GraphRetriever(s3).local_search("ws", "alice")
    s3.close()
    assert "legacy-old-uuid" not in search.evidence_chunk_ids, "legacy chunk survived (F9)"
    assert "new-uuid" in search.evidence_chunk_ids, "verified survivor dropped (F9)"
