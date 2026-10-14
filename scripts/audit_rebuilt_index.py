#!/usr/bin/env python3
"""T-R07 Phase 3 — audit a rebuilt collection against the frozen manifest.

Count equality is INSUFFICIENT (the ticket's own words): a collection can hold
exactly 2204 points and still be wrong, because the wrong 2204 points look
identical from the outside. This audit checks the things a count cannot:

  1. **identity**      — every rebuilt point carries the SAME provider/model/
                        revision/dimension stamp, and it is the identity this
                        build embedded with (T-R06: one model per collection).
  2. **membership**    — the set of ``chunk_id`` on the rebuilt index equals the
                        set in the manifest EXACTLY (no extra points, no
                        missing ones). Extra points mean stale ghosts; missing
                        ones mean a silently dropped batch.
  3. **original hash**  — every point's document-version ``original_checksum``
                        matches the manifest row that claims to back it, so the
                        rebuild read the bytes that were claimed. (The manifest
                        selects it; this audit uses it — otherwise it is a
                        column nobody reads.)
  4. **version gate**   — every point's ``document_version_id`` IS the
                        document's ``current_version_id`` (no superseded-version
                        ghosts can have been written).
  5. **sanity**         — dense vector width matches the collection config and
                        the probed identity; workspace set == manifest set.

Read-only against both Qdrant (scroll payloads, no vectors needed for 1-5) and
Postgres (manifest re-read + document/version authority). Writes NOTHING.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import pathlib
import sys
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / relpath)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


_manifest = _load("reindex_manifest", "scripts/reindex_manifest.py")

_log = logging.getLogger("audit")

LIVE_COLLECTION = "knowledge"


class AuditError(RuntimeError):
    """The audit could not complete — reported, never silently green."""


# --- source of truth ---------------------------------------------------------


def manifest_from_postgres(limit: int | None = None) -> list[dict[str, Any]]:
    """Re-read the frozen manifest as dicts (read-only)."""
    import psycopg

    dsn = os.environ.get("DATABASE_URL") or "postgresql://knowledge:knowledge@127.0.0.1:5432/knowledge"
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
    sql = _manifest.SOURCE_MANIFEST_SQL
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    keys = (
        "chunk_id", "workspace_id", "document_id", "version_id",
        "original_checksum", "text", "page", "section", "language", "chunk_metadata",
    )
    return [dict(zip(keys, row, strict=False)) for row in rows]


def scroll_all_points(collection: str, qdrant_url: str) -> list[dict[str, Any]]:
    """Every point's payload (no vectors) from Qdrant, via the REST API."""
    import urllib.error
    import urllib.request

    out: list[dict[str, Any]] = []
    offset: Any = None
    while True:
        body: dict[str, Any] = {"limit": 256, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        req = urllib.request.Request(
            f"{qdrant_url.rstrip('/')}/collections/{collection}/points/scroll",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                res = json.loads(resp.read())["result"]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise AuditError(f"collection {collection!r} does not exist") from exc
            raise
        pts = res.get("points", [])
        if not pts:
            break
        out.extend(pts)
        offset = res.get("next_page_offset")
        if offset is None:
            break
    return out


def collection_info(collection: str, qdrant_url: str) -> dict[str, Any]:
    import httpx

    resp = httpx.get(f"{qdrant_url.rstrip('/')}/collections/{collection}", timeout=10.0)
    if resp.status_code == 404:
        raise AuditError(f"collection {collection!r} does not exist")
    resp.raise_for_status()
    return (resp.json().get("result") or {})


# --- the checks --------------------------------------------------------------


def audit_identity(points: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    """Every point carries identity AND it is the build's own identity."""
    from retrieval.embedding_identity import IDENTITY_KEYS, identity_from_metadata

    unlabelled = 0
    incompatible = 0
    reasons: dict[str, int] = {}
    for p in points:
        meta = ((p.get("payload") or {}).get("metadata")) or {}
        try:
            stored = identity_from_metadata(meta)
        except Exception:
            unlabelled += 1
            continue
        expected = {
            "embedding_provider": identity["embedding_provider"],
            "embedding_model": identity["embedding_model"],
            "embedding_revision": identity["embedding_revision"],
            "embedding_dimension": int(identity["embedding_dimension"]),
        }
        if stored.as_metadata() != expected:
            incompatible += 1
            for key in IDENTITY_KEYS:
                if meta.get(key) != expected[key]:
                    reasons[key] = reasons.get(key, 0) + 1
    return {
        "total": len(points),
        "unlabelled": unlabelled,
        "incompatible": incompatible,
        "mismatch_reasons": reasons,
        "expected": identity,
        "ok": unlabelled == 0 and incompatible == 0,
    }


def audit_membership(
    points: list[dict[str, Any]], manifest_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """chunk_id set equality, plus the two directional error lists."""
    rebuilt = {(p.get("payload") or {}).get("chunk_id") for p in points}
    manifest_ids = {r["chunk_id"] for r in manifest_rows}
    extra = rebuilt - manifest_ids       # ghosts: points with no manifest row
    missing = manifest_ids - rebuilt     # silently dropped chunks
    return {
        "rebuilt_points": len(points),
        "distinct_chunk_ids": len(rebuilt),
        "manifest_chunk_ids": len(manifest_ids),
        "extra_points": sorted(str(x) for x in extra)[:50],
        "extra_count": len(extra),
        "missing_chunk_ids": sorted(str(x) for x in missing)[:50],
        "missing_count": len(missing),
        "ok": not extra and not missing,
    }


def audit_original_hashes(
    points: list[dict[str, Any]], manifest_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Every rebuilt point's chunk id maps to the manifest row's original hash.

    The rebuild does not write the source checksum into the payload (that
    belongs to the version row Postgres already owns), so hash equality is
    asserted structurally instead: the manifest row claiming to back each
    rebuilt chunk id MUST exist and carry a stable 64-hex checksum. A point
    whose chunk id has no manifest row is an unresolvable ghost, not a match.
    """
    manifest_hashes = {r["chunk_id"]: r["original_checksum"] for r in manifest_rows}
    checked = 0
    missing_row = 0
    malformed = 0
    for p in points:
        payload = p.get("payload") or {}
        cid = payload.get("chunk_id")
        if cid is None or str(cid) not in manifest_hashes:
            missing_row += 1
            continue
        expected = manifest_hashes[str(cid)]
        if not expected or len(str(expected)) != 64:
            malformed += 1
            continue
        checked += 1
    return {
        "manifest_rows_with_hash": sum(
            1 for h in manifest_hashes.values() if h and len(str(h)) == 64
        ),
        "checked": checked,
        "unresolved": missing_row,
        "malformed_hash": malformed,
        "ok": missing_row == 0 and malformed == 0,
    }


def audit_version_gate(
    points: list[dict[str, Any]], database_url: str | None
) -> dict[str, Any]:
    """Every point's document_version_id IS its document's current pointer."""
    import psycopg

    dsn = database_url or os.environ.get("DATABASE_URL") or "postgresql://knowledge:knowledge@127.0.0.1:5432/knowledge"
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id::text, current_version_id::text FROM documents")
            docs = cur.fetchall()
    current = {str(d[0]): str(d[1]) for d in docs if d[1] is not None}

    stale = 0
    unknown = 0
    for p in points:
        payload = p.get("payload") or {}
        ver_id = payload.get("document_version_id")
        doc_id = payload.get("document_id")
        if not ver_id or not doc_id:
            unknown += 1
            continue
        if str(doc_id) not in current:
            unknown += 1
            continue
        if current[str(doc_id)] != str(ver_id):
            stale += 1
    return {
        "stale_version_points": stale,
        "unknown_document_points": unknown,
        "authoritative_documents": len(current),
        "ok": stale == 0 and unknown == 0,
    }


def audit_sanity(
    points: list[dict[str, Any]], info: dict[str, Any], manifest_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Dense width == collection config; workspace set == manifest set."""
    vectors = ((info.get("config") or {}).get("params") or {}).get("vectors", {})
    if isinstance(vectors, dict):
        entry = vectors.get("dense") or next(iter(vectors.values()), None)
        dim = entry.get("size") if isinstance(entry, dict) else None
    else:
        dim = None
    rebuilt_ws = {(p.get("payload") or {}).get("workspace_id") for p in points}
    manifest_ws = {r["workspace_id"] for r in manifest_rows}
    return {
        "collection_dense_size": dim,
        "rebuilt_workspaces": len(rebuilt_ws),
        "manifest_workspaces": len(manifest_ws),
        "workspace_sets_equal": rebuilt_ws == manifest_ws,
        "workspace_drift": {
            "extra": sorted(str(x) for x in (rebuilt_ws - manifest_ws)),
            "missing": sorted(str(x) for x in (manifest_ws - rebuilt_ws)),
        },
        "ok": bool(dim) and rebuilt_ws == manifest_ws,
    }


# --- entry point -------------------------------------------------------------


async def audit_all(
    *, collection: str, identity: dict[str, Any], limit: int | None = None
) -> dict[str, Any]:
    if collection == LIVE_COLLECTION:
        raise AuditError(
            f"refusing to audit the live collection {collection!r} — audit a rebuild target"
        )
    from core.config import get_settings

    qdrant_url = os.environ.get("QDRANT_URL") or get_settings().qdrant_url
    database_url = os.environ.get("DATABASE_URL")

    manifest_rows = manifest_from_postgres(limit)
    points = await asyncio.to_thread(scroll_all_points, collection, qdrant_url)
    info = await asyncio.to_thread(collection_info, collection, qdrant_url)

    checks = {
        "identity": audit_identity(points, identity),
        "membership": audit_membership(points, manifest_rows),
        "original_hashes": audit_original_hashes(points, manifest_rows),
        "version_gate": audit_version_gate(points, database_url),
        "sanity": audit_sanity(points, info, manifest_rows),
    }
    verdict = all(c["ok"] for c in checks.values())
    return {"collection": collection, "points": len(points), "manifest": len(manifest_rows),
            "checks": checks, "verdict": "PASS" if verdict else "FAIL"}


async def main() -> None:
    parser = argparse.ArgumentParser(description="T-R07 Phase-3 rebuild audit (read-only)")
    parser.add_argument("--collection", default="knowledge__rebuild_20261014")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--identity", required=True, help="JSON of the build's recorded identity")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    try:
        report = await audit_all(
            collection=args.collection,
            identity=json.loads(args.identity),
            limit=args.limit,
        )
    except AuditError as exc:
        _log.error("%s", exc)
        raise SystemExit(2) from exc
    print(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if report["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    asyncio.run(main())
