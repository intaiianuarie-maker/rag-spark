"""Integration: checksum dedup, immutable versions, delete tombstone (M2-001d)."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.integration

PASSWORD = "Correct-Horse-Battery-9"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def client():
    if not (_port_open(5432) and _port_open(6379)):
        pytest.skip("postgres/redis not reachable; run `make up`")

    from app.db import run_migrations

    run_migrations()

    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _new_user(client) -> str:
    email = f"m2d-{uuid.uuid4().hex}@example.test"
    registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _create_workspace(client, token: str) -> str:
    response = client.post("/v1/workspaces", json={"name": "Dedup"}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(
    client,
    token: str,
    workspace_id: str,
    filename: str,
    data: bytes,
    content_type: str = "text/plain",
):
    return client.post(
        f"/v1/workspaces/{workspace_id}/documents",
        files={"file": (filename, data, content_type)},
        headers=_auth(token),
    )


def _run_job(job_id: str) -> None:
    from jobs.ingest_worker import process_one

    asyncio.run(process_one(uuid.UUID(job_id)))


def _db_scalar(query, params):
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run():
        async with get_session_factory()() as session:
            return (await session.execute(text(query), params)).scalar_one()

    return asyncio.run(run())


def _chunk_count(version_id: str) -> int:
    return int(
        _db_scalar(
            "SELECT count(*) FROM chunks WHERE document_version_id = :v", {"v": version_id}
        )
    )


def _version_number(version_id: str) -> int:
    return int(
        _db_scalar(
            "SELECT version_number FROM document_versions WHERE id = :v", {"v": version_id}
        )
    )


def _is_superseded(version_id: str) -> bool:
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run():
        async with get_session_factory()() as session:
            value = (
                await session.execute(
                    text("SELECT superseded_at FROM document_versions WHERE id = :v"),
                    {"v": version_id},
                )
            ).scalar_one()
            return value is not None

    return asyncio.run(run())


def _is_deleted(document_id: str) -> bool:
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def run():
        async with get_session_factory()() as session:
            value = (
                await session.execute(
                    text("SELECT deleted_at FROM documents WHERE id = :d"), {"d": document_id}
                )
            ).scalar_one()
            return value is not None

    return asyncio.run(run())


def _fts_count(document_id: str) -> int:
    return int(
        _db_scalar(
            "SELECT count(*) FROM lexical_docs WHERE document_id = :d", {"d": document_id}
        )
    )


def _qdrant_count(workspace_id: str, document_id: str) -> int:
    """Live Qdrant points for one document (Q3: must be zero after delete)."""
    from qdrant_client.http.models import FieldCondition, Filter, MatchAny
    from retrieval.adapters.qdrant_store import QdrantVectorStore

    async def run() -> int:
        store = QdrantVectorStore(collection="knowledge")
        try:
            points, _ = await store._client.scroll(
                collection_name="knowledge",
                scroll_filter=Filter(
                    must=[
                        FieldCondition(key="workspace_id", match=MatchAny(any=[workspace_id])),
                        FieldCondition(key="document_id", match=MatchAny(any=[document_id])),
                    ]
                ),
                limit=100,
                with_payload=False,
                with_vectors=False,
            )
            return len(points)
        finally:
            await store.close()

    return asyncio.run(run())


def _search_results(client, token: str, workspace_id: str, query: str, mode: str = "keyword"):
    response = client.post(
        f"/v1/workspaces/{workspace_id}/search",
        json={"query": query, "mode": mode},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()["results"]


def _version_count(document_id: str) -> int:
    return int(
        _db_scalar(
            "SELECT count(*) FROM document_versions WHERE document_id = :d", {"d": document_id}
        )
    )


def test_identical_reupload_skips_parse_and_reuses_version(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    first = _upload(client, token, workspace, "doc.txt", b"identical content").json()
    _run_job(first["job_id"])
    version_id = first["version_id"]
    baseline = _chunk_count(version_id)
    assert baseline >= 1

    second = _upload(client, token, workspace, "doc.txt", b"identical content").json()
    assert second["status"] == "ready"  # short-circuited, never enqueued
    assert second["version_id"] == version_id
    assert _chunk_count(version_id) == baseline  # no re-parse / duplication
    assert _version_count(first["document_id"]) == 1


def test_changed_file_creates_immutable_version_and_supersedes(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    first = _upload(client, token, workspace, "doc.txt", b"version one content").json()
    _run_job(first["job_id"])
    old_version = first["version_id"]
    assert _chunk_count(old_version) >= 1

    second = _upload(client, token, workspace, "doc.txt", b"version two changed content").json()
    assert second["document_id"] == first["document_id"]
    assert second["version_id"] != old_version
    assert second["status"] == "queued"
    _run_job(second["job_id"])

    assert _version_number(second["version_id"]) == 2
    assert _chunk_count(second["version_id"]) >= 1
    assert _chunk_count(old_version) == 0  # superseded, not mutated
    assert _is_superseded(old_version) is True
    assert _version_count(first["document_id"]) == 2


def test_delete_tombstones_and_purges_postgres(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    uploaded = _upload(client, token, workspace, "del.txt", b"to be deleted").json()
    _run_job(uploaded["job_id"])
    document_id = uploaded["document_id"]
    assert _chunk_count(uploaded["version_id"]) >= 1

    response = client.delete(
        f"/v1/workspaces/{workspace}/documents/{document_id}", headers=_auth(token)
    )
    assert response.status_code == 204
    assert _chunk_count(uploaded["version_id"]) == 0
    assert _is_deleted(document_id) is True

    # A re-upload after deletion is a fresh document, not a dedup hit.
    again = _upload(client, token, workspace, "del.txt", b"to be deleted").json()
    assert again["document_id"] != document_id


def test_delete_purges_fts_and_qdrant_and_is_unretrievable(client) -> None:
    """Q3 deletion lifecycle: tombstone propagates to FTS + Qdrant; zero hits after.

    Uploads uniquely-phrased content, verifies it is indexed and searchable,
    deletes it, then asserts: chunks gone, ``lexical_docs`` rows gone, Qdrant
    points gone, keyword search returns zero hits for the deleted document,
    and the detail read is an honest 404 (tombstone, never served as live).
    A second (live) document stays fully retrievable, proving the purge was
    document-scoped and not a workspace wipe.
    """
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    marker = f"tombstone probe {uuid.uuid4().hex} xylophone quasar"
    live_marker = f"live control {uuid.uuid4().hex} xylophone quasar"

    doomed = _upload(client, token, workspace, "doomed.txt", marker.encode()).json()
    _run_job(doomed["job_id"])
    live = _upload(client, token, workspace, "live.txt", live_marker.encode()).json()
    _run_job(live["job_id"])

    doomed_id, live_id = doomed["document_id"], live["document_id"]
    assert _chunk_count(doomed["version_id"]) >= 1
    assert _fts_count(doomed_id) >= 1
    assert _qdrant_count(workspace, doomed_id) >= 1
    pre_ids = [item["document_id"] for item in _search_results(client, token, workspace, marker)]
    assert doomed_id in pre_ids

    response = client.delete(
        f"/v1/workspaces/{workspace}/documents/{doomed_id}", headers=_auth(token)
    )
    assert response.status_code == 204, response.text

    assert _is_deleted(doomed_id) is True
    assert _chunk_count(doomed["version_id"]) == 0
    assert _fts_count(doomed_id) == 0
    assert _qdrant_count(workspace, doomed_id) == 0

    post_ids = [item["document_id"] for item in _search_results(client, token, workspace, marker)]
    assert doomed_id not in post_ids
    assert post_ids == []

    # The live control document is untouched: rows, points, and search all intact.
    assert _is_deleted(live_id) is False
    assert _chunk_count(live["version_id"]) >= 1
    assert _fts_count(live_id) >= 1
    assert _qdrant_count(workspace, live_id) >= 1
    live_ids = [
        item["document_id"] for item in _search_results(client, token, workspace, live_marker)
    ]
    assert live_id in live_ids

    detail = client.get(
        f"/v1/workspaces/{workspace}/documents/{doomed_id}", headers=_auth(token)
    )
    assert detail.status_code == 404
    assert detail.json()["detail"]["error"] == "document_not_found"


def test_delete_surfaces_partial_purge_failure_as_207(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: a failing store purger is visible as 207 + per-store report, never a false 204.

    The tombstone still commits (the document reads 404), but the response
    must name the failed store so operators know derived rows may remain.
    """
    from ingestion import dedup

    async def boom_purger(ws_id, doc_id) -> None:
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(dedup, "_STORE_PURGERS", {"boom": boom_purger})
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    uploaded = _upload(client, token, workspace, "doomed.txt", b"to be deleted").json()
    _run_job(uploaded["job_id"])
    document_id = uploaded["document_id"]

    response = client.delete(
        f"/v1/workspaces/{workspace}/documents/{document_id}", headers=_auth(token)
    )
    assert response.status_code == 207, response.text
    body = response.json()
    assert body["status"] == "degraded"
    assert body["error"] == "store_purge_failed"
    assert body["stores"]["postgres"] is True
    assert body["stores"]["boom"] is False
    assert "boom" in body["failed_stores"]

    # The tombstone stands regardless: the document is gone for reads.
    assert _is_deleted(document_id) is True
    detail = client.get(
        f"/v1/workspaces/{workspace}/documents/{document_id}", headers=_auth(token)
    )
    assert detail.status_code == 404
    assert detail.json()["detail"]["error"] == "document_not_found"


def test_delete_requires_workspace_access(client) -> None:
    owner = _new_user(client)
    outsider = _new_user(client)
    workspace = _create_workspace(client, owner)
    uploaded = _upload(client, owner, workspace, "x.txt", b"secret").json()
    response = client.delete(
        f"/v1/workspaces/{workspace}/documents/{uploaded['document_id']}",
        headers=_auth(outsider),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "not_a_member"


def test_failed_then_reupload_reparses_instead_of_false_ready(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    corrupt = b"%PDF-1.4\nthis is not a real pdf"
    first = _upload(client, token, workspace, "broken.pdf", corrupt, "application/pdf").json()
    _run_job(first["job_id"])
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async def job_state(job_id: str) -> str:
        async with get_session_factory()() as session:
            return (
                await session.execute(
                    text("SELECT status FROM ingestion_jobs WHERE id = :j"), {"j": job_id}
                )
            ).scalar_one()

    assert asyncio.run(job_state(first["job_id"])) == "failed"
    assert _chunk_count(first["version_id"]) == 0

    second = _upload(client, token, workspace, "broken.pdf", corrupt, "application/pdf").json()
    # Must NOT report a dishonest `ready`; the same version is re-enqueued.
    assert second["status"] == "queued", second
    assert second["version_id"] == first["version_id"]
    _run_job(second["job_id"])
    assert _chunk_count(first["version_id"]) == 0  # still corrupt -> still no chunks


def test_out_of_order_supersede_keeps_newer_version(client) -> None:
    token = _new_user(client)
    workspace = _create_workspace(client, token)
    first = _upload(client, token, workspace, "doc.txt", b"version one").json()
    second = _upload(client, token, workspace, "doc.txt", b"version two changed").json()
    old_version, new_version = first["version_id"], second["version_id"]

    # Process the NEWER job first, then the older one (retry-reorder scenario).
    _run_job(second["job_id"])
    newer_chunks = _chunk_count(new_version)
    assert newer_chunks >= 1
    assert _is_superseded(old_version) is True

    _run_job(first["job_id"])

    # The older completion must not clobber the newer version.
    assert _chunk_count(new_version) == newer_chunks
    assert _is_superseded(new_version) is False


def test_cross_workspace_dedup_is_isolated(client) -> None:
    token = _new_user(client)
    workspace_a = _create_workspace(client, token)
    workspace_b = _create_workspace(client, token)
    payload = b"identical bytes for the A/B dedup test"

    a = _upload(client, token, workspace_a, "same.txt", payload).json()
    b = _upload(client, token, workspace_b, "same.txt", payload).json()

    assert a["document_id"] != b["document_id"]
    assert a["version_id"] != b["version_id"]
    _run_job(a["job_id"])
    _run_job(b["job_id"])
    assert _chunk_count(a["version_id"]) >= 1
    assert _chunk_count(b["version_id"]) >= 1


def _graph_live_rows(workspace_id: str, document_id: str) -> int:
    """Live (non-tombstoned) graph rows whose provenance names this document."""
    import json
    import sqlite3

    from graph.factory import graph_db_path

    conn = sqlite3.connect(graph_db_path())
    try:
        total = 0
        for table in ("graph_entities", "graph_relationships"):
            payloads = conn.execute(
                "SELECT payload FROM " + table + " WHERE workspace_id=? AND tombstoned=0",
                (workspace_id,),
            ).fetchall()
            total += sum(
                1
                for (payload,) in payloads
                if document_id in json.loads(payload).get("document_ids", [])
            )
        return total
    finally:
        conn.close()


def _seed_graph_rows(workspace_id: str, document_id: str) -> int:
    """Insert two entities + one relationship bound to the document (G2 store)."""
    from graph.factory import graph_db_path
    from graph.interfaces import Entity, Relationship
    from graph.sqlite_store import SQLiteGraphStore

    suffix = uuid.uuid4().hex[:12]
    entity = Entity(
        id=f"ent-{suffix}",
        name=f"Late Toggle {suffix}",
        workspace_id=workspace_id,
        document_ids=[document_id],
    )
    peer = Entity(
        id=f"ent-peer-{suffix}",
        name=f"Late Toggle Peer {suffix}",
        workspace_id=workspace_id,
        document_ids=[document_id],
    )
    rel = Relationship(
        id=f"rel-{suffix}",
        source_entity_id=entity.id,
        target_entity_id=peer.id,
        workspace_id=workspace_id,
        document_ids=[document_id],
    )

    async def run() -> None:
        store = SQLiteGraphStore(graph_db_path())
        try:
            await store.upsert_entities(workspace_id, [entity, peer])
            await store.upsert_relationships(workspace_id, [rel])
        finally:
            store.close()

    asyncio.run(run())
    return 3


def test_late_graph_mode_toggle_still_purges_graph_rows(
    client, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """T-V1-007 (RP-04-lite, QF-04): re-evaluate graph purger registration every call.

    Purge once with ``GRAPH_MODE=off`` — that first purge lazily registers
    fts/qdrant, so the registry is non-empty from then on. Toggling
    ``GRAPH_MODE=rule`` on *late* must still register the graph purger on the
    next delete: the second document's graph rows are tombstoned and the
    response stays a clean 204 (207 semantics unchanged — nothing failed). A
    counting spy pins exactly one graph purge per delete (no double-purge),
    and toggling the mode back off unregisters the purger again so a stale
    closure never fires on the third document (still 204, rows untouched).
    """
    from graph.sqlite_store import SQLiteGraphStore
    from ingestion import dedup

    monkeypatch.setenv("GRAPH_DB_PATH", str(tmp_path / "graph.db"))
    monkeypatch.setenv("GRAPH_MODE", "off")

    token = _new_user(client)
    workspace = _create_workspace(client, token)

    # Phase 1 — graph OFF: this delete registers fts/qdrant (registry becomes
    # non-empty) but must never register the graph purger.
    first = _upload(client, token, workspace, "late-toggle-1.txt", b"first document").json()
    first_response = client.delete(
        f"/v1/workspaces/{workspace}/documents/{first['document_id']}",
        headers=_auth(token),
    )
    assert first_response.status_code == 204, first_response.text
    assert "graph" not in dedup.registered_stores()

    # Phase 2 — seed graph rows for a second document (as the rule-mode
    # extractor would persist them), still with the toggle off.
    second = _upload(client, token, workspace, "late-toggle-2.txt", b"second document").json()
    second_id = second["document_id"]
    assert _seed_graph_rows(workspace, second_id) == 3
    assert _graph_live_rows(workspace, second_id) == 3

    # Phase 3 — LATE toggle on: registration must be re-evaluated on this
    # call even though the registry has been non-empty since phase 1.
    monkeypatch.setenv("GRAPH_MODE", "rule")
    purges: list[tuple[str, str]] = []
    real_tombstone = SQLiteGraphStore.tombstone_document

    async def _counting_tombstone(self, ws_id: str, doc_id: str) -> int:
        purges.append((ws_id, doc_id))
        return await real_tombstone(self, ws_id, doc_id)

    monkeypatch.setattr(SQLiteGraphStore, "tombstone_document", _counting_tombstone)

    response = client.delete(
        f"/v1/workspaces/{workspace}/documents/{second_id}", headers=_auth(token)
    )
    assert response.status_code == 204, response.text  # 204/207 semantics unchanged
    assert "graph" in dedup.registered_stores()  # late registration took effect
    assert purges == [(workspace, second_id)]  # exactly one purge, no double-purge
    assert _graph_live_rows(workspace, second_id) == 0  # graph rows tombstoned

    # Phase 4 — toggle back OFF: the purger must unregister again so a stale
    # graph purge never fires; the third document's graph rows survive its
    # graph-off delete and the status stays 204.
    third = _upload(client, token, workspace, "late-toggle-3.txt", b"third document").json()
    third_id = third["document_id"]
    assert _seed_graph_rows(workspace, third_id) == 3
    monkeypatch.setenv("GRAPH_MODE", "off")

    purges.clear()
    third_response = client.delete(
        f"/v1/workspaces/{workspace}/documents/{third_id}", headers=_auth(token)
    )
    assert third_response.status_code == 204, third_response.text
    assert "graph" not in dedup.registered_stores()
    assert purges == []  # no stale graph purge after the toggle back off
    assert _graph_live_rows(workspace, third_id) == 3  # rows untouched

