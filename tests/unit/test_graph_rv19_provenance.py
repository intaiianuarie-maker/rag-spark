"""Adversarial regressions (museR) for POLICE F7-community and F9.

dev2's equivalents (test_rv19_f7_community_summary_cleared, test_rv19_f9_optional_no_document_id)
PASS on a8414fd where the defects exist (no teeth), so museR authored these from its harness.
Each FAILS on a8414fd and passes on the fix.
"""

import uuid

from graph.interfaces import Entity, GraphCommunity
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
