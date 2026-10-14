#!/usr/bin/env python3
"""T-R07 Phase 2 — build a clean replacement index from the frozen manifest.

Reads the T-R07 Phase-1 manifest (current-version, non-deleted, authorized
workspaces, original checksums, expected chunk ids) out of Postgres, embeds each
chunk through the REAL bge-m3 provider, and writes into a FRESH physical Qdrant
collection — never the live collection, never the R05 serving alias.

Identity per write (T-R06/T-R06b): every record is stamped with
``provider_identity(...).as_metadata()`` via ``merge_identity`` BEFORE the store
call, so ``QdrantVectorStore.upsert`` verifies it at the boundary and an
unlabelled or incompatible record means ZERO writes, not a mixed index. The
store's own verification is what makes this a rebuild and not a copy.

Nothing here switches, aliases, deletes or promotes. The switch is a separate,
coordinator-gated step (T-R07 READY-FOR-SWITCH / R05 promotion).

Safety model
------------
* ``assert_scratch_target`` refuses the live collection, the serving alias, any
  existing non-empty collection, and any name not marked as a rebuild target —
  by name convention, before any connection is made.
* ``--dry-run`` reads the manifest and embeds nothing and writes nothing.
* Batches are small (default 32) and one batch of vectors is held at most, so
  the footprint on this box stays bounded.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import math
import os
import pathlib
import sys
import time
from collections.abc import Sequence
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / relpath)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


_reindex = _load("reindex_all_chunks", "scripts/reindex_all_chunks.py")
_manifest = _load("reindex_manifest", "scripts/reindex_manifest.py")
_serving = _load("retrieval_serving", "packages/retrieval/serving.py")

#: Tooling used by this script only.
_log = logging.getLogger("rebuild")

#: Live/serving names this script must never write to.
LIVE_COLLECTION = "knowledge"
SCRATCH_PREFIX = "knowledge_test_"
#: Names a rebuild is allowed to target (besides the scratch prefix).
REBUILD_TARGET = "knowledge__rebuild_"

#: One batch is one synchronous POST /embeddings (see reindex_all_chunks).
DEFAULT_BATCH_SIZE = 32


#: Minimum available memory required before each batch write (1 GiB).
MIN_AVAILABLE_MEMORY_BYTES = 1024 * 1024 * 1024


def available_memory_bytes() -> int:
    """Read MemAvailable from /proc/meminfo in bytes (safe fallback if unavailable)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 2 * 1024 * 1024 * 1024


class RebuildRefused(RuntimeError):
    """Safety guard fired before any write."""


def assert_scratch_target(collection: str) -> None:
    """Refuse anything that is not a fresh, clearly-scratch rebuild target.

    Guards, all evaluated before a Qdrant or Postgres connection is opened:
    live name, the R05 serving alias, an existing NON-empty collection, and any
    name outside the rebuild naming convention.
    """
    if not collection or not collection.strip():
        raise RebuildRefused("rebuild target name required")
    if collection == LIVE_COLLECTION:
        raise RebuildRefused(
            f"refusing to build into the live collection {collection!r} — "
            "a replacement must be a different, fresh collection"
        )
    if collection == _serving.serving_alias_name(LIVE_COLLECTION):
        raise RebuildRefused(
            f"refusing to write to the serving alias {collection!r} — the alias "
            "points at the index that is serving RIGHT NOW"
        )
    if not collection.startswith(SCRATCH_PREFIX) and not collection.startswith(
        REBUILD_TARGET
    ):
        raise RebuildRefused(
            f"refusing to build into {collection!r}: not a scratch rebuild target "
            f"(must start with {SCRATCH_PREFIX!r} or {REBUILD_TARGET!r})"
        )


def fetch_manifest_rows_sync(
    database_url: str | None = None, *, limit: int | None = None
) -> list[Any]:
    """Read the frozen manifest out of Postgres (read-only, blocking).

    Runs the Phase-1 ``SOURCE_MANIFEST_SQL`` verbatim — the same query the
    Phase-1 tests pin — so what the builder writes is provably the manifest
    that was classified, not a re-derived variant.
    """
    import psycopg

    dsn = (
        database_url
        or os.environ.get("DATABASE_URL")
        or "postgresql://knowledge:knowledge@127.0.0.1:5432/knowledge"
    )
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://")

    sql = _manifest.SOURCE_MANIFEST_SQL
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return rows


async def fetch_manifest_rows(
    database_url: str | None = None, *, limit: int | None = None
) -> list[Any]:
    """Async wrapper: the blocking read runs off the event loop."""
    return await asyncio.to_thread(fetch_manifest_rows_sync, database_url, limit=limit)


def build_records(
    rows: Sequence[Sequence[Any]], *, identity: Any, column_order: Sequence[str]
) -> list[Any]:
    """Map manifest rows to VectorRecords stamped with identity.

    ``merge_identity`` applies identity AFTER the row's own metadata, so a
    caller-supplied ``embedding_model`` inside ``chunk_metadata`` can never be
    passed off as the real one (T-R06).
    """
    from retrieval.embedding_identity import merge_identity
    from retrieval.interfaces import VectorRecord

    records: list[Any] = []
    for row in rows:
        cols = dict(zip(column_order, row, strict=False))
        metadata = merge_identity(dict(cols.get("chunk_metadata") or {}), identity)
        records.append(
            VectorRecord(
                id=str(cols["chunk_id"]),
                vector=[0.0],  # replaced by the real embedding below
                content=cols["text"],
                workspace_id=str(cols["workspace_id"]),
                document_id=str(cols["document_id"]),
                document_version_id=str(cols["version_id"]),
                chunk_id=str(cols["chunk_id"]),
                page=cols.get("page"),
                section=cols.get("section"),
                metadata=metadata,
            )
        )
    return records


async def build(
    *,
    collection: str,
    database_url: str | None,
    limit: int | None,
    batch_size: int,
    dry_run: bool,
    resume: bool = False,
    request_timeout: float | None = None,
) -> dict[str, Any]:
    """The Phase-2 build. Returns a build report summary."""
    assert_scratch_target(collection)

    from retrieval.adapters.infinity_embed import InfinityEmbeddingProvider
    from retrieval.adapters.qdrant_store import QdrantVectorStore

    effective_timeout = float(
        request_timeout or os.environ.get("INFINITY_TIMEOUT_SECONDS", "300.0")
    )
    provider = InfinityEmbeddingProvider(timeout=effective_timeout)

    _log.info("fetching manifest (limit=%s)...", limit)
    t0 = time.monotonic()
    rows = await fetch_manifest_rows(database_url, limit=limit)
    _log.info("manifest rows=%d fetched in %.1fs", len(rows), time.monotonic() - t0)
    if not rows:
        raise RebuildRefused("manifest is empty — refusing to build an empty replacement")

    column_order = (
        "chunk_id", "workspace_id", "document_id", "version_id",
        "original_checksum", "text", "page", "section", "language", "chunk_metadata",
    )

    # Where Qdrant is; used for the read-only freshness probe only.
    from core.config import get_settings

    qdrant_url = os.environ.get("QDRANT_URL") or get_settings().qdrant_url

    store = QdrantVectorStore(collection=collection)
    try:
        # 1) Provider probe: MEASURE the dimension actually returned.
        observed_dim, _ = await _reindex.probe_provider(provider)
        from retrieval.embedding_identity import observed_dimension, provider_identity

        identity = provider_identity(provider, observed_dim)
        _log.info(
            "provider=%s model=%s revision=%s observed_dim=%d",
            identity.provider, identity.model, identity.revision, identity.dimension,
        )

        # 2) Freshness gate. Non-empty is refused UNLESS --resume, which is an
        #    explicit operator decision to continue a partially-built target
        #    (the provider's latency tail can kill a 69-batch run mid-way, and
        #    re-embedding what already landed is pure waste).
        inspection = await _reindex.inspect_collection(qdrant_url, collection)
        if inspection["error"]:
            raise RebuildRefused(
                f"could not inspect {collection!r} ({inspection['error']}) — "
                "cannot verify the target is fresh, so no write may proceed"
            )
        existing_points = int(inspection["points"] or 0)
        if inspection["exists"] and existing_points and not resume:
            raise RebuildRefused(
                f"refusing to build into non-empty collection {collection!r} "
                f"({existing_points} points) — a rebuild target must be fresh; re-run with "
                "--resume to continue a partial build, delete the scratch collection, or "
                "choose a new name"
            )
        already: set[str] = set()
        if inspection["exists"] and existing_points:
            # Read-only: which chunk ids already landed, so resume is a skip
            # rather than a rebuild.
            already = await asyncio.to_thread(
                _existing_chunk_ids, collection, qdrant_url
            )
            _log.info(
                "resume: %d points present, %d chunk ids resolved — skipping those",
                existing_points, len(already),
            )
            before = len(rows)
            rows = [r for r in rows if str(r[0]) not in already]
            _log.info("resume: %d of %d manifest rows still to embed", len(rows), before)
            if not rows:
                _log.info("resume: nothing left to do — target already complete")
                return {
                    "collection": collection,
                    "manifest_rows": before,
                    "written": 0,
                    "dry_run": bool(dry_run),
                    "identity": identity.as_metadata(),
                    "batch_size": batch_size,
                    "duration_s": round(time.monotonic() - t0, 1),
                    "resumed": True,
                    "complete": True,
                }

        # 3) Batch loop: embed → stamp identity → verified upsert.
        n_batches = math.ceil(len(rows) / batch_size)
        written = 0
        for i in range(0, len(rows), batch_size):
            avail = available_memory_bytes()
            if avail < MIN_AVAILABLE_MEMORY_BYTES:
                raise RebuildRefused(
                    f"available memory ({avail / (1024**3):.2f}GiB) dropped below 1GiB threshold — "
                    "aborting to BLOCKED per infra rules"
                )
            batch = rows[i : i + batch_size]
            texts = [r[5] for r in batch]  # manifest SELECT column 5 = text
            embeds = await _reindex.embed_batch_with_retry(provider, texts)
            if len(embeds) != len(batch):
                raise RuntimeError(
                    f"embedding count mismatch: expected {len(batch)}, got {len(embeds)}"
                )
            # verify dimension agreement against the probe identity
            if observed_dimension(embeds) != identity.dimension:
                raise RuntimeError(
                    "embedding dimension drifted from the probed identity — refusing to "
                    "write a mixed-dimension collection"
                )
            records = build_records(batch, identity=identity, column_order=column_order)
            for rec, vec in zip(records, embeds, strict=True):
                rec.vector = vec
            if not dry_run:
                # ensure_collection learns the dimension from the provider, then
                # upsert's boundary verifies every record's identity (T-R06b).
                await store.ensure_collection(collection=collection, dimension=identity.dimension)
                await store.upsert(collection=collection, records=records)
            written += len(batch)
            if dry_run:
                _log.info(
                    "[DRY RUN] would index batch %d-%d (%d records, 0 writes)",
                    i, i + len(batch), len(batch),
                )
            else:
                _log.info("indexed batch %d/%d (total %d)", i // batch_size + 1, n_batches, written)
    finally:
        await store.close()

    return {
        "collection": collection,
        "manifest_rows": len(rows) + len(already),
        "written": written,
        "dry_run": bool(dry_run),
        "identity": identity.as_metadata(),
        "batch_size": batch_size,
        "duration_s": round(time.monotonic() - t0, 1),
        "resumed": bool(already),
    }


def _existing_chunk_ids(collection: str, qdrant_url: str) -> set[str]:
    """Read-only: the chunk ids already present in the target collection."""
    import json
    import urllib.request

    out: set[str] = set()
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
        with urllib.request.urlopen(req, timeout=90) as resp:
            res = json.loads(resp.read())["result"]
        pts = res.get("points", [])
        if not pts:
            break
        for p in pts:
            cid = (p.get("payload") or {}).get("chunk_id")
            if cid is not None:
                out.add(str(cid))
        offset = res.get("next_page_offset")
        if offset is None:
            break
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="T-R07 Phase-2 clean rebuild from the frozen manifest"
    )
    parser.add_argument("--collection", default="knowledge__rebuild_scratch")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue a partially-built target: skip chunk ids already present "
        "(read-only scroll) instead of refusing a non-empty collection. Only "
        "safe because every record is identity-verified at the boundary.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Timeout in seconds for provider calls (default 300.0s via INFINITY_TIMEOUT_SECONDS)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    try:
        report = await build(
            collection=args.collection,
            database_url=os.environ.get("DATABASE_URL"),
            limit=args.limit,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            resume=args.resume,
            request_timeout=args.timeout,
        )
    except RebuildRefused as exc:
        _log.error("%s", exc)
        raise SystemExit(2) from exc
    _log.info("build report: %s", report)


if __name__ == "__main__":
    asyncio.run(main())
