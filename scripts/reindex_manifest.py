#!/usr/bin/env python3
"""T-R07 Phase 1 — frozen source manifest + orphan classification (read-only).

This does NOT write to any collection and does NOT embed anything. It defines
the *manifest* — the exact, tightened set of Postgres chunk rows a clean rebuild
is allowed to read from — and classifies the orphan set against dev2's
`scripts/reconcile_points_chunks.py` classification precedence, which this
module imports **rather than reimplements**.

Why "frozen": a manifest is a point-in-time authorization. It says, for THIS
rebuilt collection, exactly which (workspace, document-version, original hash,
chunk id) tuples were emitted. Phase 3 audits the rebuilt collection against
THIS manifest, not against a re-derived query.

Tightening vs. the legacy reindex query (`scripts/reindex_all_chunks.py`, line
~472): the legacy query gated on `d.deleted_at IS NULL` only, which admits chunks
belonging to a SUPERSEDED version of a live document — exactly the gap the 10-14
review's 67 `superseded-version` orphans came from. The manifest adds the two
current-version predicates and also selects the original `checksum` + `chunk_id`
so an audit can prove the rebuild read the bytes it claimed to.

Reuse, not reimplement:
* dev2's `classify_orphan_point` precedence (superseded-version > tombstoned-doc
  > no-PG-row > other) — imported.
* the 10-14 review evidence reconciliation snapshot (parsed, never rewritten).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_reconcile_module() -> Any:
    """Import `scripts/reconcile_points_chunks.py` the way its tests do.

    `scripts/` is not a package, so a plain `import` would not resolve under
    pytest's `pythonpath = ["packages","apps/api","apps/worker","."]`; mirror
    T-V1-073's test loader to reuse dev2's classifier without copying it.
    """
    spec = importlib.util.spec_from_file_location(
        "reconcile_points_chunks", _REPO_ROOT / "scripts" / "reconcile_points_chunks.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec_module: dev2's @dataclass decorators resolve their
    # module namespace via sys.modules[cls.__module__], which is None otherwise
    # (observed on 3.14) and crashes the whole load.
    sys.modules.setdefault("reconcile_points_chunks", module)
    spec.loader.exec_module(module)
    return module


_RECONCILE = _load_reconcile_module()
#: Re-export dev2's classification API verbatim — one source of truth.
classify_orphan_point = _RECONCILE.classify_orphan_point
CATEGORIES = _RECONCILE.CATEGORIES  # ("superseded-version","tombstoned-doc","no-PG-row","other")
PRUNABLE = _RECONCILE.PRUNABLE  # excludes "other" — the 40 unexplained are never prunable
CATEGORIES_SET = frozenset(CATEGORIES)
PRUNABLE_SET = frozenset(PRUNABLE)


@dataclass(frozen=True)
class ManifestRow:
    """One frozen, authorized source row for the rebuilt collection."""

    chunk_id: str
    workspace_id: str
    document_id: str
    version_id: str
    original_checksum: str
    text: str
    page: int | None
    section: str | None
    language: str | None
    chunk_metadata: dict[str, Any]


#: The frozen, tightened manifest query. This is the single place that decides
#: what a clean rebuild reads — Phase 2 adopts it; nothing else is added later.
#:
#: Tightening vs. legacy `deleted_at IS NULL` only:
#:   * `d.current_version_id IS NOT NULL` — the doc has a current version;
#:   * `d.current_version_id = dv.id` — this version IS the current one;
#:   * `dv.superseded_at IS NULL` — belt-and-suspenders: the active version is
#:     never superseded, so superseded-version orphans cannot be re-emitted.
#: `c.id` is the expected chunk id; `dv.checksum` is the original SHA-256 hash
#: the audit later matches each rebuilt point against.
SOURCE_MANIFEST_SQL = """\
SELECT
    c.id                                          AS chunk_id,
    c.workspace_id                                AS workspace_id,
    dv.document_id                                AS document_id,
    dv.id                                         AS version_id,
    dv.checksum                                   AS original_checksum,
    c.text                                        AS text,
    c.page                                        AS page,
    c.section                                     AS section,
    c.language                                    AS language,
    c.chunk_metadata                              AS chunk_metadata
FROM chunks c
JOIN document_versions dv ON c.document_version_id = dv.id
JOIN documents d              ON dv.document_id = d.id
WHERE d.deleted_at IS NULL
  AND d.current_version_id IS NOT NULL
  AND d.current_version_id = dv.id
  AND dv.superseded_at IS NULL
"""


def manifest_predicates_present(sql: str = SOURCE_MANIFEST_SQL) -> dict[str, bool]:
    """Structural guard used by the Phase-1 test: the tightening must be there.

    Asserts the manifest is NOT the legacy `deleted_at IS NULL`-only query that
    let superseded-version orphans through.
    """
    lowered = sql.lower()
    return {
        "gates_deleted_at": "deleted_at is null" in lowered,
        "requires_current_version_pointer": "current_version_id is not null" in lowered,
        "requires_current_version_match": "current_version_id = dv.id" in lowered,
        "requires_version_not_superseded": "superseded_at is null" in lowered,
        "selects_chunk_id": "as chunk_id" in lowered,
        "selects_original_checksum": "as original_checksum" in lowered,
    }


def authorized_workspaces(rows: Iterable[ManifestRow]) -> list[str]:
    """Frozen authorized workspace set: workspaces owning a current-version chunk.

    This is the manifest's answer to "authorized workspaces": only workspace ids
    that have at least one row in the frozen manifest are authorized for THIS
    rebuild; supersession/tombstones cannot leak through. Sorted for diffability.
    """
    return sorted({r.workspace_id for r in rows})


def classify_manifest_gaps(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Apply dev2's classification to the 10-14 evidence and assert preservation.

    Reuses `classify_orphan_point`'s category set + PRUNABLE (which excludes
    `other`) — never reimplements the precedence. The snapshot's `by_category`
    is the live classification; this function only asserts the hard rule:

      * the four categories sum to `orphans` (114),
      * `other` (the 40 unexplained) is present and is NOT prunable,
      * `no-PG-row` is the `pointless`/absent count, distinct from `other`.

    It does NOT fold the 1855 `chunks_without_points` into "lost production
    content" — those are Postgres chunk rows with no Qdrant point (a re-embed
    queue), not missing source bytes (the checksum column in the manifest
    exists precisely so the audit can prove that).
    """
    by_cat = dict(snapshot.get("by_category", {}))
    # Every category dev2 classifies into is present in the snapshot.
    missing = CATEGORIES_SET - set(by_cat)
    assert not missing, f"snapshot missing categories: {missing}"

    total_orphans = int(snapshot.get("orphans", -1))
    summed = sum(by_cat.values())
    assert summed == total_orphans, (
        f"category sum {summed} != reported orphans {total_orphans}"
    )

    preserved = list(_RECONCILE.CATEGORIES)  # ordered, incl. "other"
    prunable = list(_RECONCILE.PRUNABLE)
    return {
        "by_category": by_cat,
        "orphans_total": summed,
        "preserved_categories": preserved,
        "prunable_categories": prunable,
        "other_count": int(by_cat.get("other", 0)),
        "other_is_never_prunable": "other" not in PRUNABLE_SET,
        "no_pg_row_count": int(by_cat.get("no-PG-row", 0)),
        "chunks_without_points": int(snapshot.get("chunks_without_points", -1)),
        "points_matched": int(snapshot.get("matched", -1)),
        "points_total": int(snapshot.get("points", -1)),
    }


def evidence_snapshot(path: pathlib.Path | str) -> dict[str, Any]:
    """Load the 10-14 review evidence reconciliation snapshot (read-only parse).

    The snapshot already contains the live classification: points=2041,
    matched=1927, orphans=114, by_category={superseded-version:67,
    tombstoned-doc:7, no-PG-row:0, other:40}, chunks_without_points=1855.
    Phase-1 tests assert this snapshot satisfies the preserve invariant rather
    than re-deriving it (no live Postgres is required, and none is touched).
    """
    text = pathlib.Path(path).read_text(errors="replace")
    data = json.loads(text)

    def walk(node: Any) -> Any | None:
        if isinstance(node, dict):
            if "orphans" in node and "by_category" in node:
                return node
            for v in node.values():
                hit = walk(v)
                if hit:
                    return hit
        elif isinstance(node, list):
            for v in node:
                hit = walk(v)
                if hit:
                    return hit
        return None

    snap = walk(data)
    return dict(snap) if snap else {}


__all__ = [
    "CATEGORIES",
    "PRUNABLE",
    "SOURCE_MANIFEST_SQL",
    "ManifestRow",
    "authorized_workspaces",
    "classify_manifest_gaps",
    "classify_orphan_point",
    "evidence_snapshot",
    "manifest_predicates_present",
]
