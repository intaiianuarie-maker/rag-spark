"""Adversarial cross-process regression tests for POLICE findings F1/F4/F5.

Authored by museR from its adversarial harness (the same probes that reproduced the
defects on cd5be3e). Each test forces the EXACT interleaving the bug required: it freezes
the victim read, runs an independent-process writer while the parent is mid-operation, then
lets the parent finish. Each test FAILS on cd5be3e and passes on the fix.

Why subprocess + connection proxy (not asyncio.gather): SQLiteGraphStore serializes
same-process writers with a per-path lock, so an in-process race cannot reproduce the
cross-process defect.
"""

import subprocess
import sys
import uuid
from pathlib import Path

from graph.interfaces import Entity
from graph.retriever import GraphRetriever
from graph.sqlite_store import SQLiteGraphStore

REPO = Path(__file__).resolve().parents[2]

_PRE = (
    "import asyncio,sys\n"
    f"sys.path[:0]=[{str(REPO / 'packages')!r}, "
    f"{str(REPO / 'apps/worker')!r}, {str(REPO / 'apps/api')!r}]\n"
    "from graph.sqlite_store import SQLiteGraphStore\n"
    "from graph.interfaces import Entity\n"
)


def _run_child(code: str):
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()[-200:]


def _child_extract(path, eid, doc, chunk):
    return _PRE + (
        "\nasync def m():\n"
        f"    s=SQLiteGraphStore(r'{path}')\n"
        f"    e=Entity(id='{eid}',name='{eid}',workspace_id='ws',"
        f"document_ids=['{doc}'],chunk_ids=['{chunk}'])\n"
        f"    r=await s.upsert_extraction('ws',[e],[],[],document_id='{doc}')\n"
        "    print('child',r); s.close()\n"
        "asyncio.run(m())\n"
    )


def _child_entities(path, doc, chunk):
    return _PRE + (
        "\nasync def m():\n"
        f"    s=SQLiteGraphStore(r'{path}')\n"
        "    e=Entity(id='alice',name='Alice',workspace_id='ws',"
        f"document_ids=['{doc}'],chunk_ids=['{chunk}'])\n"
        "    r=await s.upsert_entities('ws',[e])\n"
        "    print('child',r); s.close()\n"
        "asyncio.run(m())\n"
    )


def _entity(eid, doc, chunk):
    return Entity(id=eid, name=eid, workspace_id="ws", document_ids=[doc], chunk_ids=[chunk])


class _FrozenCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


async def _race(store, freeze_sql, child_code, method_name, *args):
    """Freeze the first SQL starting with freeze_sql, run an independent-process writer,
    then let the parent operation finish. Returns (result, child_result)."""
    conn = store._connect()
    fired = []

    class _Proxy:
        def __enter__(self):
            conn.__enter__()
            return self

        def __exit__(self, *a):
            return conn.__exit__(*a)

        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, a=()):
            cur = conn.execute(sql, a)
            if not fired and sql.startswith(freeze_sql):
                rows = cur.fetchall()
                fired.append(True)
                fired.append(_run_child(child_code))
                return _FrozenCursor(rows)
            return cur

    store._connect = lambda: _Proxy()
    result = await getattr(store, method_name)(*args)
    return result, (fired[1] if len(fired) > 1 else None)


async def test_rv19_f1_delete_side_interleaving(tmp_path):
    """F1: an independent extraction between tombstone_document's read and write must not
    publish a live entity after the deletion."""
    db = tmp_path / "f1.sqlite"
    store = SQLiteGraphStore(db)
    await store.upsert_extraction(
        "ws", [_entity("e0", "doc", "c0")], [], [], document_id="doc",
    )
    _, child = await _race(
        store,
        "SELECT entity_id, payload FROM graph_entities",
        _child_extract(db, "fresh", "doc", "new-chunk"),
        "tombstone_document", "ws", "doc",
    )
    live = await store.get_entity("ws", "fresh")
    store.close()
    assert live is None, "live entity published after document deletion (F1)"
    assert child is not None and child[0] != 0, (
        "independent extraction was not blocked by the delete transaction (F1)"
    )


async def test_rv19_f4_tombstone_chunks_interleaving(tmp_path):
    """F4: an independent publish carrying the retiring chunk, between tombstone_chunks' read
    and its retirement write, must not resurrect a late entity."""
    db = tmp_path / "f4.sqlite"
    store = SQLiteGraphStore(db)
    await store.upsert_extraction(
        "ws", [_entity("e0", "doc", "old-chunk")], [], [], document_id="doc",
    )
    _, child = await _race(
        store,
        "SELECT entity_id, payload FROM graph_entities",
        _child_extract(db, "late", "doc", "old-chunk"),
        "tombstone_chunks", "ws", ["old-chunk"],
    )
    late = await store.get_entity("ws", "late")
    search = await GraphRetriever(store).local_search("ws", "late")
    store.close()
    assert late is None, "late entity published after chunk retirement (F4)"
    assert "old-chunk" not in search.evidence_chunk_ids, (
        "retired chunk returned after retirement (F4)"
    )
    assert child is not None and child[0] != 0, (
        "independent publish was not blocked by the retirement transaction (F4)"
    )


async def test_rv19_f5_concurrent_merge_interleaving(tmp_path):
    """F5: an independent merge between upsert_entities' read and write must not be silently
    lost. If the concurrent writer succeeds, its provenance must survive."""
    db = tmp_path / "f5.sqlite"
    store = SQLiteGraphStore(db)
    doc_a, doc_b, doc_c = (str(uuid.uuid4()) for _ in range(3))
    ch_a, ch_b, ch_c = (str(uuid.uuid4()) for _ in range(3))
    await store.upsert_entities("ws", [_entity("alice", doc_a, ch_a)])
    _, child = await _race(
        store,
        "SELECT payload FROM graph_entities",
        _child_entities(db, doc_b, ch_b),
        "upsert_entities", "ws", [_entity("alice", doc_c, ch_c)],
    )
    ent = await store.get_entity("ws", "alice")
    store.close()
    child_succeeded = child is not None and child[0] == 0
    assert (not child_succeeded) or (doc_b in ent.document_ids), (
        "concurrent writer reported success but its provenance was silently lost (F5)"
    )
