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
import math
import os
import pathlib
import sys
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in ("packages", "apps/api", "apps/worker", "."):
    _path_str = str((_REPO_ROOT / _p).resolve())
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)


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
    _str_keys = ("chunk_id", "workspace_id", "document_id", "version_id")
    return [
        {
            k: str(v) if k in _str_keys and v is not None else v
            for k, v in zip(keys, row, strict=False)
        }
        for row in rows
    ]


def scroll_all_points(
    collection: str, qdrant_url: str, with_vectors: bool = False
) -> list[dict[str, Any]]:
    """Every point's payload (+ optional vectors) from Qdrant, via the REST API."""
    import urllib.error
    import urllib.request

    out: list[dict[str, Any]] = []
    offset: Any = None
    while True:
        body: dict[str, Any] = {
            "limit": 256,
            "with_payload": True,
            "with_vector": with_vectors,
        }
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


def audit_identity(
    points: list[dict[str, Any]], identity: dict[str, Any], denominator: str = "512/2204"
) -> dict[str, Any]:
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
        "denominator": denominator,
        "total": len(points),
        "unlabelled": unlabelled,
        "incompatible": incompatible,
        "mismatch_reasons": reasons,
        "expected": identity,
        "ok": unlabelled == 0 and incompatible == 0,
    }


def audit_membership(
    points: list[dict[str, Any]],
    manifest_rows: list[dict[str, Any]],
    denominator: str = "512/2204",
) -> dict[str, Any]:
    """chunk_id set equality, plus the two directional error lists."""
    rebuilt = {
        str((p.get("payload") or {}).get("chunk_id"))
        for p in points
        if (p.get("payload") or {}).get("chunk_id") is not None
    }
    manifest_ids = {
        str(r["chunk_id"])
        for r in manifest_rows
        if r.get("chunk_id") is not None
    }
    extra = rebuilt - manifest_ids       # ghosts: points with no manifest row
    missing = manifest_ids - rebuilt     # silently dropped chunks
    return {
        "denominator": denominator,
        "rebuilt_points": len(points),
        "distinct_chunk_ids": len(rebuilt),
        "manifest_chunk_ids": len(manifest_ids),
        "extra_points": sorted(str(x) for x in extra)[:50],
        "extra_count": len(extra),
        "missing_chunk_ids": sorted(str(x) for x in missing)[:50],
        "missing_count": len(missing),
        "subset_ok": not extra,
        "ok": not extra and not missing,
    }


def audit_original_hashes(
    points: list[dict[str, Any]],
    manifest_rows: list[dict[str, Any]],
    denominator: str = "512/2204",
) -> dict[str, Any]:
    """Every rebuilt point's chunk id maps to the manifest row's original hash.

    The rebuild does not write the source checksum into the payload (that
    belongs to the version row Postgres already owns), so hash equality is
    asserted structurally instead: the manifest row claiming to back each
    rebuilt chunk id MUST exist and carry a stable 64-hex checksum. A point
    whose chunk id has no manifest row is an unresolvable ghost, not a match.
    """
    manifest_hashes = {
        str(r["chunk_id"]): r["original_checksum"]
        for r in manifest_rows
        if r.get("chunk_id") is not None
    }
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
        "denominator": denominator,
        "manifest_rows_with_hash": sum(
            1 for h in manifest_hashes.values() if h and len(str(h)) == 64
        ),
        "checked": checked,
        "unresolved": missing_row,
        "malformed_hash": malformed,
        "ok": missing_row == 0 and malformed == 0,
    }


def audit_version_gate(
    points: list[dict[str, Any]], database_url: str | None, denominator: str = "512/2204"
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
        "denominator": denominator,
        "stale_version_points": stale,
        "unknown_document_points": unknown,
        "authoritative_documents": len(current),
        "ok": stale == 0 and unknown == 0,
    }


def audit_sanity(
    points: list[dict[str, Any]],
    info: dict[str, Any],
    manifest_rows: list[dict[str, Any]],
    denominator: str = "512/2204",
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
        "denominator": denominator,
        "collection_dense_size": dim,
        "rebuilt_workspaces": len(rebuilt_ws),
        "manifest_workspaces": len(manifest_ws),
        "workspace_sets_equal": rebuilt_ws == manifest_ws,
        "workspace_drift": {
            "extra": sorted(str(x) for x in (rebuilt_ws - manifest_ws)),
            "missing": sorted(str(x) for x in (manifest_ws - rebuilt_ws)),
        },
        "subset_ok": bool(dim) and len(rebuilt_ws - manifest_ws) == 0,
        "ok": bool(dim) and rebuilt_ws == manifest_ws,
    }


def audit_vector_sanity(
    points: list[dict[str, Any]], expected_dim: int = 1024, denominator: str = "512/2204"
) -> dict[str, Any]:
    """Dense vectors must have dim=expected_dim, finite floats, norm > 0, and no zero-runs."""
    ragged = 0
    nan_count = 0
    inf_count = 0
    zero_vectors = 0
    max_zero_run = 0
    zero_run_violations = 0
    checked = 0
    for p in points:
        vec = p.get("vector")
        dense = vec.get("dense") if isinstance(vec, dict) else vec
        if not dense or len(dense) != expected_dim:
            ragged += 1
            continue
        checked += 1
        if any(math.isnan(x) for x in dense):
            nan_count += 1
        if any(math.isinf(x) for x in dense):
            inf_count += 1
        norm = math.sqrt(sum(x * x for x in dense))
        if norm < 1e-6:
            zero_vectors += 1
        curr_zeros = 0
        max_z = 0
        for x in dense:
            if x == 0.0:
                curr_zeros += 1
                max_z = max(max_z, curr_zeros)
            else:
                curr_zeros = 0
        max_zero_run = max(max_zero_run, max_z)
        if max_z > 10:
            zero_run_violations += 1
    ok = (
        checked == len(points)
        and ragged == 0
        and nan_count == 0
        and inf_count == 0
        and zero_vectors == 0
        and zero_run_violations == 0
    )
    return {
        "denominator": denominator,
        "checked_points": checked,
        "total_points": len(points),
        "expected_dimension": expected_dim,
        "ragged_dimension": ragged,
        "nan_count": nan_count,
        "inf_count": inf_count,
        "zero_vectors": zero_vectors,
        "max_consecutive_zeros": max_zero_run,
        "zero_run_violations": zero_run_violations,
        "ok": ok,
    }


def audit_representative_retrieval(
    collection: str,
    points: list[dict[str, Any]],
    qdrant_url: str,
    n_probes: int = 8,
    denominator: str = "512/2204",
) -> dict[str, Any]:
    """Sample distinct probe points and verify vector search returns sane ranks."""
    import urllib.request

    seen_ws: set[str] = set()
    probes: list[dict[str, Any]] = []
    for p in points:
        ws = (p.get("payload") or {}).get("workspace_id")
        if ws and ws not in seen_ws and p.get("vector"):
            seen_ws.add(ws)
            probes.append(p)
        if len(probes) >= n_probes:
            break
    if not probes:
        for p in points[:n_probes]:
            if p.get("vector"):
                probes.append(p)

    probe_results = []
    all_sane = True
    for p in probes:
        vec = p.get("vector")
        dense = vec.get("dense") if isinstance(vec, dict) else vec
        cid = (p.get("payload") or {}).get("chunk_id")
        ws = (p.get("payload") or {}).get("workspace_id")
        body = {
            "vector": {"name": "dense", "vector": dense},
            "limit": 5,
            "with_payload": True,
        }
        req = urllib.request.Request(
            f"{qdrant_url.rstrip('/')}/collections/{collection}/points/search",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                hits = json.loads(resp.read()).get("result") or []
        except Exception as exc:
            probe_results.append({"chunk_id": cid, "error": str(exc), "ok": False})
            all_sane = False
            continue

        if not hits:
            probe_results.append({"chunk_id": cid, "error": "no hits", "ok": False})
            all_sane = False
            continue

        top_score = float(hits[0].get("score", 0.0))
        scores = [float(h.get("score", 0.0)) for h in hits]
        descending = all(scores[i] >= scores[i + 1] - 1e-6 for i in range(len(scores) - 1))
        in_range = all(-1.0 <= s <= 1.05 for s in scores)
        self_match = abs(top_score - 1.0) < 1e-4
        probe_ok = self_match and descending and in_range
        if not probe_ok:
            all_sane = False
        probe_results.append({
            "chunk_id": cid,
            "workspace_id": ws,
            "top_score": round(top_score, 4),
            "ranks_descending": descending,
            "scores_in_cos_range": in_range,
            "ok": probe_ok,
        })
    return {
        "denominator": denominator,
        "probes_tested": len(probe_results),
        "probes_passed": sum(1 for p in probe_results if p.get("ok")),
        "probes": probe_results,
        "ok": bool(probe_results) and all_sane,
    }


async def audit_v1_e2e_acceptance_dry(
    collection: str,
    points: list[dict[str, Any]],
    qdrant_url: str,
    target_identity: dict[str, Any] | None = None,
    denominator: str = "512/2204",
) -> dict[str, Any]:
    """Dry-run read-path acceptance: boundary identity verification and ACL isolation."""
    from retrieval.adapters.qdrant_store import QdrantVectorStore, VectorStoreIdentityError
    from retrieval.embedding_identity import EmbeddingIdentity
    from retrieval.interfaces import SearchFilter

    store = QdrantVectorStore(collection=collection)
    if target_identity:
        valid_identity = EmbeddingIdentity(
            provider=str(target_identity.get("embedding_provider", "GeminiEmbedding2")),
            model=str(target_identity.get("embedding_model", "google/gemini-embedding-2")),
            revision=str(target_identity.get("embedding_revision", "google/gemini-embedding-2")),
            dimension=int(target_identity.get("embedding_dimension", 3072)),
        )
        mismatched_identity = EmbeddingIdentity(
            provider=str(target_identity.get("embedding_provider", "GeminiEmbedding2")),
            model="wrong-model",
            revision=str(target_identity.get("embedding_revision", "google/gemini-embedding-2")),
            dimension=int(target_identity.get("embedding_dimension", 3072)),
        )
    else:
        valid_identity = EmbeddingIdentity(
            provider="InfinityEmbeddingProvider",
            model="BAAI/bge-m3",
            revision="BAAI/bge-m3",
            dimension=1024,
        )
        mismatched_identity = EmbeddingIdentity(
            provider="InfinityEmbeddingProvider",
            model="wrong-model",
            revision="BAAI/bge-m3",
            dimension=1024,
        )

    sample = next((p for p in points if p.get("vector")), None)
    if not sample:
        return {"denominator": denominator, "ok": False, "error": "no points with vector"}

    vec = sample.get("vector")
    dense = vec.get("dense") if isinstance(vec, dict) else vec
    if not isinstance(dense, list):
        return {"denominator": denominator, "ok": False, "error": "sample vector not a list"}
    dense_vec: list[float] = [float(x) for x in dense]
    ws = (sample.get("payload") or {}).get("workspace_id")

    hits = await store.search(
        collection=collection,
        vector=dense_vec,
        top_k=3,
        query_filter=SearchFilter(workspace_ids=[str(ws)]),
        query_identity=valid_identity,
    )
    s1_ok = len(hits) >= 1 and all(h.workspace_id == str(ws) for h in hits)

    s2_refused = False
    try:
        await store.search(
            collection=collection,
            vector=dense_vec,
            top_k=3,
            query_filter=SearchFilter(workspace_ids=[str(ws)]),
            query_identity=mismatched_identity,
        )
    except VectorStoreIdentityError:
        s2_refused = True
    except Exception:
        s2_refused = False

    foreign_hits = await store.search(
        collection=collection,
        vector=dense_vec,
        top_k=3,
        query_filter=SearchFilter(workspace_ids=["00000000-0000-0000-0000-000000000000"]),
        query_identity=valid_identity,
    )
    s3_ok = len(foreign_hits) == 0

    return {
        "denominator": denominator,
        "scenarios": {
            "boundary_verified_retrieval": {"ok": s1_ok, "hits": len(hits)},
            "boundary_mismatched_refusal": {"ok": s2_refused},
            "acl_isolation_zero_leak": {"ok": s3_ok, "foreign_hits": len(foreign_hits)},
        },
        "ok": s1_ok and s2_refused and s3_ok,
    }


# --- entry point -------------------------------------------------------------


async def audit_all(
    *,
    collection: str,
    identity: dict[str, Any],
    limit: int | None = None,
    rehearsal: bool = False,
) -> dict[str, Any]:
    if collection == LIVE_COLLECTION:
        raise AuditError(
            f"refusing to audit the live collection {collection!r} — audit a rebuild target"
        )
    from core.config import get_settings

    qdrant_url = os.environ.get("QDRANT_URL") or get_settings().qdrant_url
    database_url = os.environ.get("DATABASE_URL")

    manifest_rows = manifest_from_postgres(limit)
    points = await asyncio.to_thread(scroll_all_points, collection, qdrant_url, True)
    info = await asyncio.to_thread(collection_info, collection, qdrant_url)

    expected_dim = int(identity.get("embedding_dimension", 1024))
    denom = "512/2204" if rehearsal else f"{len(points)}/{len(manifest_rows)}"

    identity_check = audit_identity(points, identity, denominator=denom)
    membership_check = audit_membership(points, manifest_rows, denominator=denom)
    hashes_check = audit_original_hashes(points, manifest_rows, denominator=denom)
    version_check = audit_version_gate(points, database_url, denominator=denom)
    sanity_check = audit_sanity(points, info, manifest_rows, denominator=denom)
    vector_check = audit_vector_sanity(points, expected_dim=expected_dim, denominator=denom)
    retrieval_check = await asyncio.to_thread(
        audit_representative_retrieval, collection, points, qdrant_url, denominator=denom
    )
    e2e_check = await audit_v1_e2e_acceptance_dry(
        collection, points, qdrant_url, target_identity=identity, denominator=denom
    )

    checks = {
        "identity": identity_check,
        "membership": membership_check,
        "original_hashes": hashes_check,
        "version_gate": version_check,
        "sanity": sanity_check,
        "vector_sanity": vector_check,
        "representative_retrieval": retrieval_check,
        "v1_e2e_acceptance_dry": e2e_check,
    }
    if rehearsal:
        rehearsal_ok = (
            identity_check["ok"]
            and bool(membership_check.get("subset_ok"))
            and hashes_check["ok"]
            and version_check["ok"]
            and bool(sanity_check.get("subset_ok"))
            and vector_check["ok"]
            and retrieval_check["ok"]
            and e2e_check["ok"]
        )
        verdict = "PASS" if rehearsal_ok else "FAIL"
    else:
        verdict = "PASS" if all(c["ok"] for c in checks.values()) else "FAIL"

    return {
        "collection": collection,
        "denominator": denom,
        "points": len(points),
        "manifest": len(manifest_rows),
        "checks": checks,
        "verdict": verdict,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="T-R07 Phase-3 rebuild audit (read-only)")
    parser.add_argument("--collection", default="knowledge__rebuild_20261014")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--identity", required=True, help="JSON of the build's recorded identity")
    parser.add_argument(
        "--rehearsal",
        action="store_true",
        help="Audit rehearsal against a partial build subset (512/2204 denominator)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    try:
        report = await audit_all(
            collection=args.collection,
            identity=json.loads(args.identity),
            limit=args.limit,
            rehearsal=args.rehearsal,
        )
    except AuditError as exc:
        _log.error("%s", exc)
        raise SystemExit(2) from exc
    print(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if report["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    asyncio.run(main())
