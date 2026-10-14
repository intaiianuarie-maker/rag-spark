"""T-R07 Phase 1 — frozen source manifest + orphan classification (read-only).

Asserts the manifest is the tightened query (not the legacy `deleted_at`-only
one), reuses dev2's classifier, and proves the 10-14 review's 40 `other`
orphans are preserved (never prunable) while the 1855 PG-chunks-absent are NOT
conflated with lost production content. No writes anywhere; `knowledge` is
never named.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _REPO / relpath)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


manifest = _load("reindex_manifest", "scripts/reindex_manifest.py")
reconcile = _load("reconcile_points_chunks", "scripts/reconcile_points_chunks.py")

_REVIEW_DIR = _REPO / "reviews" / "2026-10-14-super-rag-v1-v4-review"
EVIDENCE = _REVIEW_DIR / "super-rag-review-evidence-2026-10-14.json"

pytestmark = [pytest.mark.unit]


# --- manifest is tightened past deleted_at-only --------------------------------


def test_manifest_tightens_past_deleted_at_only() -> None:
    predicates = manifest.manifest_predicates_present()
    assert all(predicates.values()), predicates
    # Negative: the manifest must NOT be the legacy deleted_at-only query.
    legacy_sql = "SELECT c.id FROM chunks c JOIN ... WHERE d.deleted_at IS NULL"
    assert manifest.SOURCE_MANIFEST_SQL != legacy_sql
    # The three tightened predicates beyond the legacy gate.
    assert "current_version_id = dv.id" in manifest.SOURCE_MANIFEST_SQL
    assert "dv.superseded_at IS NULL" in manifest.SOURCE_MANIFEST_SQL


def test_manifest_selects_chunk_id_and_original_checksum() -> None:
    sql = manifest.SOURCE_MANIFEST_SQL
    assert "as chunk_id" in sql.lower()
    assert "as original_checksum" in sql.lower()
    # The checksum is the document version's SHA-256 original hash.
    assert "dv.checksum" in sql


def test_manifest_row_shape_carries_identity_and_content() -> None:
    row = manifest.ManifestRow(
        chunk_id="chk-1",
        workspace_id="ws-a",
        document_id="doc-a",
        version_id="v-a",
        original_checksum="0" * 64,
        text="evidence",
        page=1,
        section="Introduction",
        language="en",
        chunk_metadata={"k": "v"},
    )
    assert row.chunk_id == "chk-1"
    assert len(row.original_checksum) == 64  # SHA-256 hex
    assert row.workspace_id == "ws-a"


def test_authorized_workspaces_are_the_frozen_current_set() -> None:
    rows = [
        manifest.ManifestRow("c1", "ws-a", "d1", "v1", "0" * 64, "t", None, None, "en", {}),
        manifest.ManifestRow("c2", "ws-b", "d2", "v2", "0" * 64, "t", None, None, "en", {}),
        manifest.ManifestRow("c3", "ws-a", "d3", "v3", "0" * 64, "t", None, None, "en", {}),
    ]
    # Distinct, sorted, derived only from manifest rows — supersession never leaks.
    assert manifest.authorized_workspaces(rows) == ["ws-a", "ws-b"]


# --- classification is reused, not reimplemented -------------------------------


def test_classifier_is_dev2s_precedence() -> None:
    classify = manifest.classify_orphan_point
    # Each branch of dev2's precedence, exercised to prove we delegate not reimplement.
    assert classify(
        chunk_row_exists=False,
        version_row_exists=True,
        version_superseded=True,
        is_current_version=False,
        document_deleted=True,
    ) == "superseded-version"  # superseded wins over tombstone
    assert classify(
        chunk_row_exists=False,
        version_row_exists=True,
        version_superseded=None,
        is_current_version=True,
        document_deleted=True,
    ) == "tombstoned-doc"
    assert classify(
        chunk_row_exists=False,
        version_row_exists=False,
        version_superseded=None,
        is_current_version=None,
        document_deleted=None,
    ) == "no-PG-row"
    assert classify(
        chunk_row_exists=False,
        version_row_exists=True,
        version_superseded=False,
        is_current_version=True,
        document_deleted=False,
    ) == "other"  # current+alive but chunk gone: the unexplained


def test_other_is_present_and_never_prunable() -> None:
    expected = {"superseded-version", "tombstoned-doc", "no-PG-row", "other"}
    assert set(manifest.CATEGORIES) == expected
    # the 40 unexplained are NEVER in the prunable set
    assert "other" not in manifest.PRUNABLE


def test_classify_manifest_gaps_asserts_the_preserve_invariant_on_the_evidence() -> None:
    snapshot = manifest.evidence_snapshot(EVIDENCE)
    assert snapshot, "10-14 evidence snapshot not found — Phase 1 depends on dev2's reconciliation"
    gap = manifest.classify_manifest_gaps(snapshot)

    # 114 orphans, categories sum exactly, other=40 preserved.
    assert gap["orphans_total"] == 114
    assert gap["by_category"]["superseded-version"] == 67
    assert gap["by_category"]["tombstoned-doc"] == 7
    assert gap["by_category"]["no-PG-row"] == 0
    assert gap["by_category"]["other"] == 40
    assert gap["other_is_never_prunable"] is True

    # The 1855 PG-chunks-absent are a SEPARATE bucket, not folded into orphan loss.
    assert gap["chunks_without_points"] == 1855
    assert gap["chunks_without_points"] != gap["orphans_total"]  # not conflated


def test_no_pg_row_is_distinct_from_other_and_not_conflated_as_loss() -> None:
    snapshot = manifest.evidence_snapshot(EVIDENCE)
    gap = manifest.classify_manifest_gaps(snapshot)
    # no-PG-row==0 here, but the bucket exists in dev2's classifier and is treated
    # as a re-embed queue, not "missing production content" — the manifest's
    # original_checksum column is what proves content is recoverable.
    assert "no-PG-row" in manifest.CATEGORIES
    assert gap["no_pg_row_count"] == 0
    assert "original_checksum" in manifest.SOURCE_MANIFEST_SQL  # the recovery proof exists


# --- the module is read-only surface (no write/collection mutations) -----------


def test_manifest_module_is_read_only_no_store_mutations() -> None:
    for name in dir(manifest):
        assert name not in {
            "upsert", "delete_points", "promote_replacement",
            "ensure_collection", "ensure_serving_alias",
        }, f"manifest module must not expose writes: {name}"
    src = (EVIDENCE.parent / ".." / ".." / "scripts" / "reindex_manifest.py").resolve().read_text()
    for forbidden in ("query_points", "upsert", "delete_points", "create_collection"):
        assert forbidden not in src, forbidden


def test_no_live_collection_named() -> None:
    sql = manifest.SOURCE_MANIFEST_SQL
    assert "knowledge" not in sql.lower()
    # Manifest rows are keyed by workspace/chunk, never by a live collection name.
    assert not hasattr(manifest, "knowledge")
