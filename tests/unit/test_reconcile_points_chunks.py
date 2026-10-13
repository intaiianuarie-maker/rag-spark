"""T-V1-073 — covering tests for the reconciliation classifier and prune guards.

Pure logic: no Qdrant, no Postgres, no network. The classification precedence IS
the safety argument of the prune, so it is pinned here directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "reconcile_points_chunks", _REPO / "scripts" / "reconcile_points_chunks.py"
)
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
sys.modules["reconcile_points_chunks"] = mod
_SPEC.loader.exec_module(mod)

classify = mod.classify_orphan_point


# --- classification precedence ----------------------------------------------


def test_superseded_version_wins_over_tombstone() -> None:
    """A point on a replaced version of a deleted document is a superseded-version
    orphan — the version was already retired, so the point is expected garbage."""
    assert (
        classify(
            chunk_row_exists=False,
            version_row_exists=True,
            version_superseded=True,
            is_current_version=False,
            document_deleted=True,
        )
        == "superseded-version"
    )


def test_non_current_version_counts_as_superseded() -> None:
    assert (
        classify(
            chunk_row_exists=False,
            version_row_exists=True,
            version_superseded=False,
            is_current_version=False,
            document_deleted=False,
        )
        == "superseded-version"
    )


def test_tombstoned_document() -> None:
    assert (
        classify(
            chunk_row_exists=False,
            version_row_exists=True,
            version_superseded=False,
            is_current_version=True,
            document_deleted=True,
        )
        == "tombstoned-doc"
    )


def test_no_version_row_is_no_pg_row() -> None:
    assert (
        classify(
            chunk_row_exists=False,
            version_row_exists=False,
            version_superseded=None,
            is_current_version=None,
            document_deleted=None,
        )
        == "no-PG-row"
    )


def test_alive_current_version_with_missing_chunk_is_unexplained() -> None:
    """This must land in 'other' — and 'other' is never prunable."""
    assert (
        classify(
            chunk_row_exists=False,
            version_row_exists=True,
            version_superseded=False,
            is_current_version=True,
            document_deleted=False,
        )
        == "other"
    )


def test_chunk_row_present_is_not_an_orphan() -> None:
    assert (
        classify(
            chunk_row_exists=True,
            version_row_exists=True,
            version_superseded=False,
            is_current_version=True,
            document_deleted=False,
        )
        == "other"
    )


# --- prune category policy ---------------------------------------------------


def test_default_categories_exclude_other() -> None:
    assert "other" not in mod.PRUNABLE
    assert mod.resolve_prune_categories(None) == mod.PRUNABLE


def test_other_can_never_be_requested_for_pruning() -> None:
    with pytest.raises(mod.PruneRefused, match="must be investigated, not deleted"):
        mod.resolve_prune_categories(["other"])


def test_unknown_category_is_refused() -> None:
    with pytest.raises(mod.PruneRefused):
        mod.resolve_prune_categories(["everything"])


def test_known_categories_are_accepted() -> None:
    assert mod.resolve_prune_categories(["superseded-version"]) == ("superseded-version",)


# --- live-collection guards --------------------------------------------------


def test_apply_against_live_without_live_flag_is_refused() -> None:
    with pytest.raises(mod.PruneRefused, match="refusing to prune the live collection"):
        mod.assert_prune_allowed(collection="knowledge", apply=True, live=None)


def test_apply_against_scratch_without_live_flag_is_refused() -> None:
    with pytest.raises(mod.PruneRefused, match="--apply requires --live"):
        mod.assert_prune_allowed(collection="scratch_x", apply=True, live=None)


def test_live_flag_must_match_the_collection() -> None:
    with pytest.raises(mod.PruneRefused, match="does not match"):
        mod.assert_prune_allowed(collection="scratch_x", apply=True, live="knowledge")


def test_dry_run_needs_no_flags() -> None:
    mod.assert_prune_allowed(collection="knowledge", apply=False, live=None)


def test_explicit_live_opt_in_is_allowed() -> None:
    mod.assert_prune_allowed(collection="knowledge", apply=True, live="knowledge")
    mod.assert_prune_allowed(collection="scratch_x", apply=True, live="scratch_x")


def test_live_collection_is_in_the_guard_set() -> None:
    assert "knowledge" in mod.LIVE_COLLECTIONS


# --- report rendering --------------------------------------------------------


def test_report_lists_every_category_even_when_zero() -> None:
    rec = mod.Reconciliation(collection="c", total_points=5, matched_points=2)
    out = mod.render_report(rec)
    for cat in mod.CATEGORIES:
        assert cat in out
    assert "ORPHAN POINTS" in out and "pointless PG chunks" in out


def test_refusal_exits_nonzero_without_touching_anything() -> None:
    rc = mod.main(["--collection", "knowledge", "--apply"])
    assert rc == 2