"""T-R07 Phase 3 — covering tests for the rebuild audit's checks.

Pure logic: no Qdrant, no Postgres, no network. Every check is fed synthetic
points/manifest rows, including the cases that must FAIL, because a check that
only ever says OK proves nothing. Count equality is deliberately never treated
as sufficient: `test_membership_ok_requires_exact_set_equality` shows a
same-count, wrong-chunk-id set failing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _REPO / relpath)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


audit = _load("audit_rebuilt_index", "scripts/audit_rebuilt_index.py")

pytestmark = [pytest.mark.unit]

IDENTITY = {
    "embedding_provider": "InfinityEmbeddingProvider",
    "embedding_model": "BAAI/bge-m3",
    "embedding_revision": "BAAI/bge-m3",
    "embedding_dimension": 1024,
}


def point(chunk_id: str, *, version_id: str = "v1", doc_id: str = "d1",
          workspace: str = "ws-a", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": f"pt-{chunk_id}",
        "payload": {
            "chunk_id": chunk_id,
            "document_version_id": version_id,
            "document_id": doc_id,
            "workspace_id": workspace,
            "metadata": metadata if metadata is not None else dict(IDENTITY),
        },
    }


def manifest_row(chunk_id: str, *, version_id: str = "v1", doc_id: str = "d1",
                 workspace: str = "ws-a", checksum: str | None = None) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "workspace_id": workspace,
        "document_id": doc_id,
        "version_id": version_id,
        "original_checksum": checksum if checksum is not None else "a" * 64,
        "text": f"text for {chunk_id}",
        "page": None,
        "section": None,
        "language": "en",
        "chunk_metadata": {},
    }


# --- identity -----------------------------------------------------------------


def test_identity_ok_when_every_point_matches_the_build() -> None:
    r = audit.audit_identity([point("c1"), point("c2")], IDENTITY)
    assert r["ok"] and r["unlabelled"] == 0 and r["incompatible"] == 0


def test_identity_fails_on_unlabelled_point() -> None:
    r = audit.audit_identity([point("c1"), point("c2", metadata={"language": "en"})], IDENTITY)
    assert not r["ok"]
    assert r["unlabelled"] == 1
    assert r["incompatible"] == 0


def test_identity_fails_on_mismatched_model_and_names_the_field() -> None:
    meta = dict(IDENTITY, embedding_model="some-other-model")
    r = audit.audit_identity([point("c1"), point("c2", metadata=meta)], IDENTITY)
    assert not r["ok"]
    assert r["incompatible"] == 1
    assert r["mismatch_reasons"] == {"embedding_model": 1}


# --- membership: exact set equality, NOT count equality -----------------------


def test_membership_ok_on_exact_set_equality() -> None:
    pts = [point("c1"), point("c2")]
    rows = [manifest_row("c1"), manifest_row("c2")]
    r = audit.audit_membership(pts, rows)
    assert r["ok"] and r["extra_count"] == 0 and r["missing_count"] == 0


def test_membership_fails_when_counts_match_but_ids_differ() -> None:
    """THE point of this check: same count, wrong members => FAIL."""
    pts = [point("ghost-1"), point("ghost-2")]
    rows = [manifest_row("c1"), manifest_row("c2")]
    r = audit.audit_membership(pts, rows)
    assert not r["ok"], "count equality must never pass as correctness"
    assert r["extra_count"] == 2
    assert r["missing_count"] == 2
    assert set(r["extra_points"]) == {"ghost-1", "ghost-2"}
    assert set(r["missing_chunk_ids"]) == {"c1", "c2"}


def test_membership_detects_a_single_dropped_batch() -> None:
    pts = [point("c1"), point("c2")]
    rows = [manifest_row("c1"), manifest_row("c2"), manifest_row("c3")]
    r = audit.audit_membership(pts, rows)
    assert not r["ok"] and r["missing_count"] == 1
    assert r["missing_chunk_ids"] == ["c3"]


def test_membership_detects_a_ghost_point() -> None:
    pts = [point("c1"), point("ghost")]
    rows = [manifest_row("c1")]
    r = audit.audit_membership(pts, rows)
    assert not r["ok"] and r["extra_count"] == 1 and r["missing_count"] == 0


# --- original hashes ----------------------------------------------------------


def test_original_hashes_ok_when_every_chunk_id_resolves() -> None:
    pts = [point("c1"), point("c2")]
    rows = [manifest_row("c1"), manifest_row("c2")]
    r = audit.audit_original_hashes(pts, rows)
    assert r["ok"] and r["checked"] == 2 and r["unresolved"] == 0


def test_original_hashes_fails_on_unresolvable_ghost() -> None:
    pts = [point("c1"), point("ghost")]
    rows = [manifest_row("c1")]
    r = audit.audit_original_hashes(pts, rows)
    assert not r["ok"] and r["unresolved"] == 1


def test_original_hashes_rejects_a_malformed_hash() -> None:
    pts = [point("c1")]
    rows = [manifest_row("c1", checksum="tooshort")]
    r = audit.audit_original_hashes(pts, rows)
    assert not r["ok"] and r["malformed_hash"] == 1


# --- version gate -------------------------------------------------------------


async def test_version_gate_rejects_the_live_collection_outright() -> None:
    """Auditing the live index is refused before any read happens."""
    with pytest.raises(audit.AuditError) as exc:
        await audit.audit_all(collection="knowledge", identity=IDENTITY)
    assert "live collection" in str(exc.value)


# --- sanity -------------------------------------------------------------------


def test_sanity_ok_when_workspaces_match_and_dim_present() -> None:
    pts = [point("c1"), point("c2")]
    rows = [manifest_row("c1"), manifest_row("c2")]
    info = {"config": {"params": {"vectors": {"dense": {"size": 1024}}}}}
    r = audit.audit_sanity(pts, info, rows)
    assert r["ok"] and r["collection_dense_size"] == 1024


def test_sanity_fails_on_workspace_drift() -> None:
    pts = [point("c1", workspace="ws-a"), point("c2", workspace="ws-b")]
    rows = [manifest_row("c1", workspace="ws-a"), manifest_row("c2", workspace="ws-a")]
    r = audit.audit_sanity(pts, {}, rows)
    assert not r["ok"]
    assert r["workspace_sets_equal"] is False
    # rebuilt={ws-a, ws-b}; manifest={ws-a} → extra ws-b, nothing missing.
    assert r["workspace_drift"]["extra"] == ["ws-b"]
    assert r["workspace_drift"]["missing"] == []


# --- module surface -----------------------------------------------------------


def test_audit_module_writes_nothing_to_qdrant() -> None:
    src = (_REPO / "scripts" / "audit_rebuilt_index.py").read_text()
    # An audit may only read. Any of these would make it a writer.
    for forbidden in ("upsert", "promote_replacement", "ensure_serving_alias",
                      "delete_collection", "update_collection_aliases", "create_collection"):
        assert forbidden not in src, f"audit must not {forbidden}"
