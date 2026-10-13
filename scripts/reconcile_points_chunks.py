#!/usr/bin/env python3
"""Reconcile Qdrant points ⇄ Postgres chunk rows, with a guarded orphan prune.

T-V1-073. Read-only by default; the delete path is opt-in and refuses the live
collection unless the operator spells it out.

Why this lives in ``scripts/`` rather than /tmp: the T-036 acceptance record has
to cite a *reproducible* reconciliation command, and the classification logic
has to be importable by ``tests/unit/test_reconcile_points_chunks.py``. A
one-shot in /tmp would satisfy neither after the next box rebuild.

Safety model
------------
* **Dry run is the default.** ``--apply`` is required to delete anything.
* **``--apply`` alone is refused.** Deleting requires ``--apply`` *and*
  ``--live <collection>``, so a copy-pasted apply against production fails
  closed instead of pruning the live index.
* **The live collection name is guarded.** ``knowledge`` is the production
  collection; it cannot be pruned without ``--live knowledge`` spelled out.
* **Categories are explicit.** Only the named categories are deleted; anything
  unclassified is reported and never deleted, because "we could not explain
  this point" is a reason to investigate, not to delete.
* The report itself only ever *reads* (``scroll`` with payloads, no vectors).

Classification of an orphan point (a point whose ``chunk_id`` has no PG row)
is decided in this precedence:

``superseded-version`` → the version was replaced (``superseded_at`` set, or it
is not the document's ``current_version_id``); else ``tombstoned-doc`` → the
document is deleted (``deleted_at`` set); else ``no-PG-row`` → neither the
chunk row nor a usable version row exists; else ``other`` → version row is
alive and current but the chunk row is gone, which is unexplained.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

DEFAULT_COLLECTION = "knowledge"
#: Production collection names that may never be pruned without ``--live``.
LIVE_COLLECTIONS = frozenset({"knowledge"})

CATEGORIES = ("superseded-version", "tombstoned-doc", "no-PG-row", "other")
#: Categories the prune is willing to delete. ``other`` is deliberately absent.
PRUNABLE = ("superseded-version", "tombstoned-doc", "no-PG-row")


class PruneRefused(RuntimeError):
    """The operator asked for something the safety model forbids."""


# --- data model --------------------------------------------------------------


@dataclass
class OrphanPoint:
    point_id: int | str
    chunk_id: str | None
    document_id: str | None
    document_version_id: str | None
    category: str
    detail: str = ""


@dataclass
class Reconciliation:
    collection: str
    total_points: int = 0
    matched_points: int = 0
    orphans: list[OrphanPoint] = field(default_factory=list)
    pointless_chunks: int = 0
    pointless_by_slice: dict[str, int] = field(default_factory=dict)

    @property
    def by_category(self) -> dict[str, int]:
        """Every category, including zeros.

        A consumer must be able to diff two runs without special-casing a
        missing key, so a category with no orphans reads as 0, not absent.
        """
        counts = Counter(o.category for o in self.orphans)
        return {cat: counts.get(cat, 0) for cat in CATEGORIES}


# --- classification (pure; unit-tested without any service) -------------------


def classify_orphan_point(
    *,
    chunk_row_exists: bool,
    version_row_exists: bool,
    version_superseded: bool | None,
    is_current_version: bool | None,
    document_deleted: bool | None,
) -> str:
    """Return the orphan category for one point.

    ``version_superseded`` is ``version_versions.superseded_at IS NOT NULL``;
    ``is_current_version`` is the document's ``current_version_id`` match;
    ``document_deleted`` is ``documents.deleted_at IS NOT NULL``. ``None`` means
    "unknown because the row does not exist".

    Kept free of I/O so the precedence is directly testable — this ordering is
    the whole safety argument of the prune.
    """
    if chunk_row_exists:
        # Not actually orphaned by chunk identity; caller mis-classified.
        return "other"
    if version_superseded is True or (is_current_version is False and version_row_exists):
        return "superseded-version"
    if document_deleted is True:
        return "tombstoned-doc"
    if not version_row_exists:
        return "no-PG-row"
    return "other"


# --- I/O ---------------------------------------------------------------------


def _qdrant_scroll(url: str, collection: str) -> list[dict[str, Any]]:
    """Scroll every point's payload (no vectors — they are not needed)."""
    out: list[dict[str, Any]] = []
    offset: int | None = None
    while True:
        body: dict[str, Any] = {"limit": 256, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        req = urllib.request.Request(
            f"{url}/collections/{collection}/points/scroll",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                res = json.loads(resp.read())["result"]
        except urllib.error.URLError as exc:  # pragma: no cover - operator error path
            raise RuntimeError(f"Qdrant scroll failed for {collection!r}: {exc}") from exc
        pts = res.get("points", [])
        if not pts:
            break
        out.extend(pts)
        offset = res.get("next_page_offset")
        if offset is None:
            break
    return out


def _pg_snapshot(
    database_url: str,
) -> tuple[set[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (chunk_ids, version_id -> info, document_id -> info)."""
    import psycopg

    chunk_ids: set[str] = set()
    versions: dict[str, dict[str, Any]] = {}
    docs: dict[str, dict[str, Any]] = {}
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT id::text FROM chunks")
        chunk_ids = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT id::text, document_id::text, (superseded_at IS NOT NULL) FROM document_versions"
        )
        versions = {r[0]: {"document_id": r[1], "superseded": r[2]} for r in cur.fetchall()}
        cur.execute(
            "SELECT id::text, current_version_id::text, (deleted_at IS NOT NULL) FROM documents"
        )
        docs = {r[0]: {"current_version_id": r[1], "deleted": r[2]} for r in cur.fetchall()}
    return chunk_ids, versions, docs


def reconcile(
    *,
    collection: str = DEFAULT_COLLECTION,
    qdrant_url: str | None = None,
    database_url: str | None = None,
) -> Reconciliation:
    """Full read-only join of points against chunk rows, versions and tombstones."""
    qurl = (qdrant_url or os.environ.get("QDRANT_URL") or "http://localhost:6333").rstrip("/")
    dsn = (
        database_url
        or os.environ.get("DATABASE_URL")
        or "postgresql://knowledge:knowledge@localhost:5432/knowledge"
    )
    parsed = urlparse(dsn)
    if parsed.scheme not in ("postgresql", "postgresql+psycopg"):
        raise RuntimeError(f"unsupported database_url scheme {parsed.scheme!r}; refusing to guess")

    chunk_ids, versions, docs = _pg_snapshot(dsn)
    points = _qdrant_scroll(qurl, collection)

    rec = Reconciliation(collection=collection, total_points=len(points))
    seen_chunks: set[str] = set()
    for p in points:
        payload = p.get("payload") or {}
        cid = payload.get("chunk_id")
        cid = str(cid) if cid is not None else None
        doc_id = payload.get("document_id")
        ver_id = payload.get("document_version_id")
        doc_id = str(doc_id) if doc_id is not None else None
        ver_id = str(ver_id) if ver_id is not None else None

        if cid is not None:
            seen_chunks.add(cid)
        if cid is not None and cid in chunk_ids:
            rec.matched_points += 1
            continue

        version = versions.get(ver_id) if ver_id else None
        doc = docs.get(version["document_id"]) if version else None
        doc = doc or (docs.get(doc_id) if doc_id else None)
        category = classify_orphan_point(
            chunk_row_exists=False,
            version_row_exists=version is not None,
            version_superseded=version["superseded"] if version else None,
            is_current_version=(
                (doc["current_version_id"] == ver_id) if (doc and ver_id) else None
            ),
            document_deleted=doc["deleted"] if doc else None,
        )
        detail = ""
        if version is None:
            detail = "no document_versions row for the point's version id"
        elif doc is None:
            detail = "no documents row for the point's document id"
        elif version["superseded"]:
            detail = "version superseded (superseded_at set)"
        elif doc["deleted"]:
            detail = "document tombstoned (deleted_at set)"
        else:
            detail = "version is current and document alive, yet the chunk row is gone"
        rec.orphans.append(
            OrphanPoint(
                point_id=p.get("id"),
                chunk_id=cid,
                document_id=doc_id,
                document_version_id=ver_id,
                category=category,
                detail=detail,
            )
        )

    pointless = chunk_ids - seen_chunks
    rec.pointless_chunks = len(pointless)
    return rec


# --- prune -------------------------------------------------------------------


def resolve_prune_categories(requested: list[str] | None) -> tuple[str, ...]:
    """Validate ``--categories``; ``other`` is never prunable."""
    if not requested:
        return PRUNABLE
    bad = [c for c in requested if c not in PRUNABLE]
    if bad:
        raise PruneRefused(
            f"refusing categories {bad}: not prunable. Allowed: {list(PRUNABLE)}. "
            "'other' means unexplained and must be investigated, not deleted."
        )
    return tuple(requested)


def assert_prune_allowed(*, collection: str, apply: bool, live: str | None) -> None:
    """Fail closed unless the operator asked, twice, on purpose."""
    if not apply:
        return
    if collection in LIVE_COLLECTIONS and live is None:
        raise PruneRefused(
            f"refusing to prune the live collection {collection!r} without --live "
            f"{collection}. Re-run with --apply --live {collection} if that is "
            "genuinely intended, or use a scratch collection."
        )
    if live is None:
        raise PruneRefused("--apply requires --live <collection> to be spelled out")
    if live != collection:
        raise PruneRefused(
            f"--live {live!r} does not match --collection {collection!r}; refusing to prune"
        )


def render_report(rec: Reconciliation) -> str:
    lines = [
        f"collection            : {rec.collection}",
        f"points in collection  : {rec.total_points}",
        f"points matched to PG  : {rec.matched_points}",
        f"ORPHAN POINTS         : {len(rec.orphans)}",
    ]
    for cat in CATEGORIES:
        lines.append(f"    {cat:<20}: {rec.by_category.get(cat, 0)}")
    lines.append(f"pointless PG chunks   : {rec.pointless_chunks}")
    return "\n".join(lines)


def delete_points(*, qurl: str, collection: str, point_ids: list[Any]) -> int:
    if not point_ids:
        return 0
    req = urllib.request.Request(
        f"{qurl}/collections/{collection}/points/delete",
        data=json.dumps({"points": point_ids}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        json.loads(resp.read())
    return len(point_ids)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--collection", default=DEFAULT_COLLECTION)
    ap.add_argument("--qdrant-url", default=None)
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--apply", action="store_true", help="actually delete (requires --live)")
    ap.add_argument("--live", default=None, help="spell out the collection to prune")
    ap.add_argument("--categories", nargs="*", default=None)
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)

    try:
        assert_prune_allowed(collection=args.collection, apply=args.apply, live=args.live)
        categories = resolve_prune_categories(args.categories)
    except PruneRefused as exc:
        print(f"REFUSED: {exc}")
        return 2

    rec = reconcile(
        collection=args.collection,
        qdrant_url=args.qdrant_url,
        database_url=args.database_url,
    )

    doomed = [o for o in rec.orphans if o.category in categories]

    if args.json:
        # Machine-readable contract: JSON only, nothing after it.
        print(
            json.dumps(
                {
                    "collection": rec.collection,
                    "total_points": rec.total_points,
                    "matched_points": rec.matched_points,
                    "orphan_points": len(rec.orphans),
                    "by_category": rec.by_category,
                    "pointless_chunks": rec.pointless_chunks,
                    "prunable": [o.point_id for o in doomed],
                    "applied": bool(args.apply),
                },
                indent=2,
            )
        )
        return 0

    print(render_report(rec))
    print()

    doomed = [o for o in rec.orphans if o.category in categories]
    print()
    print(f"would delete           : {len(doomed)} (categories: {', '.join(categories)})")
    for cat in categories:
        ids = [o for o in doomed if o.category == cat]
        if not ids:
            continue
        print(f"  {cat} ({len(ids)}):")
        for o in ids[:10]:
            print(f"      point={o.point_id} chunk={o.chunk_id} :: {o.detail}")
        if len(ids) > 10:
            print(f"      ... and {len(ids) - 10} more")

    if not args.apply:
        print()
        print("DRY RUN — nothing deleted. Re-run with --apply --live <collection> to act.")
        return 0

    qurl = (args.qdrant_url or os.environ.get("QDRANT_URL") or "http://localhost:6333").rstrip("/")
    n = delete_points(qurl=qurl, collection=args.collection, point_ids=[o.point_id for o in doomed])
    print(f"\nAPPLIED — deleted {n} points from {args.collection!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())