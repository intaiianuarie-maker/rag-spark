"""Reindex all database chunks into Postgres FTS (lexical_docs) and Qdrant.

T-V1-014 — reindex through the REAL validated embedding provider (never zeros):

* **No zero-vector repair path.** Vectors come only from
  ``InfinityEmbeddingProvider``; ``validate_vector`` rejects wrong-dimension,
  non-finite and all-zero vectors before a record is even built, and the
  provider itself refuses zero vectors — there is no fallback that can
  manufacture an embedding.
* **Provider identity is recorded.** Every written record carries
  ``embedding_provider`` / ``embedding_model`` / ``embedding_revision`` /
  ``embedding_dimension`` in its metadata, so later runs (and auditors) can
  tell which model built the index.
* **Never mix models.** Before a real run the target collection is inspected
  read-only; existing vectors are kept (in-place re-embed allowed) only when
  model identity *and* dimensions verify against the current provider.
  Unknown-model or mismatched indexes are never "repaired" by writing a
  different model into them — the run refuses and points at the recovery
  procedure below.
* **``--dry-run`` reports and never writes.** It embeds every batch through
  the real provider to *measure* the dimensions actually returned (never
  assuming 1024), reports provider / model / dims / counts, and performs zero
  store writes: no collection create, no FTS insert, no Qdrant upsert, no
  alias change.
* **Build-then-switch recovery runs through the verified switch (T-V1-045).**
  ``--replacement <name>`` builds the new index under ``<name>`` and hands the
  switch to ``QdrantVectorStore.promote_replacement`` (T-V1-015): that call
  verifies the replacement read-only first (exists / dense dims / point count)
  and only then changes any alias, so nothing here can rename or delete a
  collection before the replacement is proven. This script has no other
  destructive route: without ``--replacement`` it does not switch at all.

Failed-rebuild recovery (T-V1-014, mechanised by T-V1-045)
----------------------------------------------------------
1. **Refused in-place run** (unknown or mismatched identity): nothing was
   written — the existing index still serves unchanged. Build a replacement
   and let the script make the verified switch::

       python scripts/reindex_all_chunks.py --collection knowledge --replacement knowledge_v2

   Every write targets ``knowledge_v2``; the live ``knowledge`` is never
   written to. When the build finishes, the script calls
   ``promote_replacement(live="knowledge", replacement="knowledge_v2",
   expected_points=<chunks written>)``, which verifies the replacement
   read-only **before** any destructive call — so an interrupted or failed
   rebuild, or a replacement that never reaches the expected point count,
   leaves ``knowledge`` serving exactly as it did before. ``--dry-run``
   prints the same switch plan (and, when the run would refuse, reports the
   switch as blocked) with zero writes: no build, no alias change.
   Without ``--replacement`` the script only reports the plan — the switch
   stays an operator/coordinator config change.
2. **Interrupted or failed rebuild**: writes only ever targeted the
   replacement collection, so the previous index is untouched and keeps
   serving. The half-written replacement is unusable — delete that collection
   and re-run the rebuild (a refused replacement is reported, never written
   into); never finish it by writing a second model into it.
3. **Identity gap to close**: points ingested by the worker/API do not carry
   the identity stamp yet (that path is outside this script's allowlist), so
   they read as ``unknown`` and force the fresh-collection route above until
   identity stamping lands in the ingest path.
"""

import argparse
import asyncio
import logging
import math
import os
from typing import Any
from urllib.parse import quote

import httpx
from core.config import get_settings
from ingestion.persistence import get_session_factory
from retrieval.adapters.infinity_embed import InfinityEmbeddingProvider
from retrieval.adapters.pg_fts import PostgresLexicalStore
from retrieval.adapters.qdrant_store import QdrantVectorStore, VectorStoreError
from retrieval.interfaces import VectorRecord
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reindex")

#: Fixed probe input used to *measure* the provider's real output dimensions.
_DIMENSION_PROBE = "T-V1-014 dimension probe"

#: Sentinel for existing points that carry no recorded identity at all.
_UNRECORDED = "<unrecorded>"


class ReindexRefused(RuntimeError):
    """Safety guard fired: writing would mix models or cannot be verified."""


def validate_vector(vector: list[float], expected_dim: int) -> None:
    """Validate vector length, finiteness, and ensure it is not all zeros."""
    if len(vector) != expected_dim:
        raise ValueError(f"Vector dimension mismatch: got {len(vector)}, expected {expected_dim}")
    if any(not math.isfinite(x) for x in vector):
        raise ValueError("Vector contains non-finite values (NaN or Inf)")
    if all(x == 0.0 for x in vector):
        raise ValueError("Manufactured all-zero vector rejected")


async def probe_provider(provider: InfinityEmbeddingProvider) -> tuple[int, int]:
    """Embed one probe text through the REAL provider.

    Returns ``(observed_dim, nonzero_dim)`` — the dimensions the provider
    *actually* returns (measured, never assumed: the served model may be
    smaller than the configured dimension and padded by the adapter, so the
    non-zero count is reported alongside to expose the real served size).
    """
    vectors = await provider.embed_documents([_DIMENSION_PROBE])
    if len(vectors) != 1:
        raise RuntimeError(f"provider probe returned {len(vectors)} vectors for 1 input")
    vector = vectors[0]
    validate_vector(vector, provider.dimension)
    return len(vector), sum(1 for x in vector if x != 0.0)


def provider_identity(provider: InfinityEmbeddingProvider, observed_dim: int) -> dict[str, Any]:
    """Provider identity stamped into every record's metadata (T-V1-014 metric)."""
    return {
        "embedding_provider": type(provider).__name__,
        "embedding_model": provider.model_name,
        "embedding_revision": provider.revision,
        "embedding_dimension": int(observed_dim),
    }


def build_record(row: Any, vector: list[float], identity: dict[str, Any]) -> VectorRecord:
    """Map a DB chunk + validated vector to a VectorRecord stamped with identity."""
    return VectorRecord(
        id=str(row.id),
        vector=vector,
        workspace_id=str(row.workspace_id),
        document_id=str(row.document_id),
        document_version_id=str(row.document_version_id),
        chunk_id=str(row.id),
        content=row.text,
        page=row.page,
        section=row.section,
        metadata={
            "language": row.language or "en",
            **(row.chunk_metadata or {}),
            # Identity always wins over any document-supplied keys.
            **identity,
        },
    )


def _identity_scan_cap() -> int:
    """Max existing points to verify for model identity (env-overridable)."""
    try:
        return max(1, int(os.environ.get("REINDEX_IDENTITY_SCAN_CAP", "10000")))
    except ValueError:
        return 10000


async def inspect_collection(base_url: str, collection: str) -> dict[str, Any]:
    """Read-only inspection of the target Qdrant collection (T-V1-014 guard).

    Performs **no writes**: only GET collection info plus payload-only scroll
    to sample the recorded model identity of existing points. Connection or
    payload problems are recorded in ``error`` instead of raised, so callers
    can refuse safely ("cannot verify" ⇒ never write blind).
    """
    info: dict[str, Any] = {
        "exists": False,
        "points": 0,
        "dims": None,
        "checked": 0,
        "models": [],
        "revisions": [],
        "recorded_dims": [],
        "error": None,
    }
    url = base_url.rstrip("/") + "/collections/" + quote(collection, safe="")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return info
            resp.raise_for_status()
            result = resp.json().get("result") or {}
            info["exists"] = True
            vectors = (result.get("config") or {}).get("params", {}).get("vectors", {})
            if isinstance(vectors, dict) and "size" in vectors:
                info["dims"] = vectors.get("size")
            elif isinstance(vectors, dict):
                entry = vectors.get("dense") or next(iter(vectors.values()), None)
                info["dims"] = entry.get("size") if isinstance(entry, dict) else None
            info["points"] = int(result.get("points_count") or 0)

            cap = min(info["points"], _identity_scan_cap())
            offset: Any = None
            models: set[str] = set()
            revisions: set[str] = set()
            recorded_dims: set[str] = set()
            while info["checked"] < cap:
                body: dict[str, Any] = {
                    "limit": min(1000, cap - info["checked"]),
                    "with_payload": ["metadata"],
                    "with_vector": False,
                }
                if offset is not None:
                    body["offset"] = offset
                scroll = await client.post(url + "/points/scroll", json=body)
                scroll.raise_for_status()
                page = scroll.json().get("result") or {}
                points = page.get("points") or []
                if not points:
                    break
                for point in points:
                    meta = ((point.get("payload") or {}).get("metadata")) or {}
                    model = meta.get("embedding_model")
                    revision = meta.get("embedding_revision")
                    dim = meta.get("embedding_dimension")
                    models.add(model if isinstance(model, str) else _UNRECORDED)
                    revisions.add(revision if isinstance(revision, str) else _UNRECORDED)
                    recorded_dims.add(str(dim) if dim is not None else _UNRECORDED)
                    info["checked"] += 1
                offset = page.get("next_page_offset")
                if offset is None:
                    break
            info["models"] = sorted(models)
            info["revisions"] = sorted(revisions)
            info["recorded_dims"] = sorted(recorded_dims)
    except Exception as exc:  # never fail inspection loudly — refusing is the safe outcome
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def decide_action(
    inspection: dict[str, Any], observed_dim: int, identity: dict[str, Any]
) -> tuple[str, str]:
    """Decide what a run may do with the target collection: build/keep/refuse.

    ``keep`` (in-place re-embed) is allowed **only** when model identity and
    dimensions verify; an unknown-model index is never mixed with new-model
    vectors to "repair" it (T-V1-014).
    """
    if inspection["error"]:
        return (
            "refuse",
            f"could not inspect the existing index ({inspection['error']}) — "
            "cannot verify model identity, so no write may proceed",
        )
    if not inspection["exists"]:
        return "build", "collection does not exist yet"
    if inspection["dims"] != observed_dim:
        return (
            "refuse",
            f"collection dims {inspection['dims']!r} != provider dims {observed_dim} — "
            "an existing index is never resized/adapted in place; rebuild into a fresh collection",
        )
    if not inspection["points"]:
        return "build", "collection exists but holds no points"
    if inspection["checked"] < inspection["points"]:
        return (
            "refuse",
            f"model identity verified for only {inspection['checked']}/{inspection['points']} "
            "points (scan cap reached) — raise REINDEX_IDENTITY_SCAN_CAP or rebuild fresh",
        )
    models = set(inspection["models"])
    revisions = set(inspection["revisions"])
    recorded_dims = set(inspection["recorded_dims"])
    if (
        _UNRECORDED in models
        or _UNRECORDED in revisions
        or _UNRECORDED in recorded_dims
    ):
        return (
            "refuse",
            "existing index has no recorded model identity (unknown-model index) — "
            "never mix models to repair it; rebuild into a fresh collection",
        )
    expected_model = str(identity["embedding_model"])
    expected_revision = str(identity["embedding_revision"])
    expected_dim = str(identity["embedding_dimension"])
    if (
        models == {expected_model}
        and revisions == {expected_revision}
        and recorded_dims == {expected_dim}
    ):
        return (
            "keep",
            f"existing vectors verify: model={expected_model} revision={expected_revision} "
            f"dims={expected_dim} ({inspection['checked']} points checked)",
        )
    return (
        "refuse",
        f"model identity mismatch: index models={sorted(models)} "
        f"revisions={sorted(revisions)} dims={sorted(recorded_dims)} vs provider "
        f"model={expected_model} revision={expected_revision} dims={expected_dim} — "
        "never mix models",
    )


async def reindex(
    *,
    dry_run: bool = False,
    batch_size: int = 100,
    collection: str = "knowledge",
    replacement: str | None = None,
) -> int:
    # T-V1-045 guard: a switch needs two different names, and "switch onto
    # itself" would delete the live index for nothing.
    if replacement is not None and (not replacement.strip() or replacement == collection):
        raise ReindexRefused(
            f"invalid replacement {replacement!r} for live collection {collection!r}: "
            "it must be a different, non-empty name — promote_replacement verifies one "
            "collection and switches it onto another, never onto itself"
        )

    session_factory = get_session_factory()
    embed_provider = InfinityEmbeddingProvider()

    # 1) REAL provider probe — measure the dims it returns, never assume 1024.
    observed_dim, nonzero_dim = await probe_provider(embed_provider)
    identity = provider_identity(embed_provider, observed_dim)
    adaptation = os.environ.get("ALLOW_DIMENSION_ADAPTATION", "false")
    logger.info(
        "provider: %s base_url=%s model=%s revision=%s",
        type(embed_provider).__name__,
        embed_provider.base_url,
        identity["embedding_model"],
        identity["embedding_revision"],
    )
    logger.info(
        "dims: configured=%d observed=%d nonzero=%d adaptation=ALLOW_DIMENSION_ADAPTATION=%s",
        embed_provider.dimension,
        observed_dim,
        nonzero_dim,
        adaptation,
    )
    if nonzero_dim < observed_dim:
        logger.warning(
            "provider returned %d non-zero dims of %d — served model is smaller than the "
            "configured dims (padding active; T-V1-043 owns the bge-m3 fix)",
            nonzero_dim,
            observed_dim,
        )
    logger.info("identity recorded on write: %s", identity)

    # 2) Read-only inspection + decision — BEFORE any write.
    # With --replacement the REPLACEMENT is the decision target: the live
    # collection is written by nothing here, so it is never inspected for a
    # write decision (promote_replacement verifies it read-only at switch time).
    target = collection if replacement is None else replacement
    qdrant_url = os.environ.get("QDRANT_URL") or get_settings().qdrant_url
    inspection = await inspect_collection(qdrant_url, target)
    action, reason = decide_action(inspection, observed_dim, identity)
    logger.info(
        "collection %r: exists=%s points=%s dims=%s checked=%s/%s models=%s revisions=%s "
        "recorded_dims=%s error=%s",
        target,
        inspection["exists"],
        inspection["points"],
        inspection["dims"],
        inspection["checked"],
        inspection["points"],
        inspection["models"],
        inspection["revisions"],
        inspection["recorded_dims"],
        inspection["error"],
    )
    logger.info("plan: action=%s — %s", action, reason)
    if replacement is not None and action != "refuse":
        # Announced BEFORE any write: the operator sees the switch (and that it
        # retires the live collection) while stopping is still free.
        logger.info(
            "switch plan (T-V1-015/T-V1-045): build/verify %r, then "
            "promote_replacement(live=%r, replacement=%r, expected_points=<chunks written>) "
            "— verification is read-only and runs first; %r is never written to here",
            replacement,
            collection,
            replacement,
            collection,
        )

    if not dry_run and action == "refuse":
        raise ReindexRefused(
            f"refusing to reindex into {target!r}: {reason}. Never mix models to repair "
            "an unknown-model index (T-V1-014). Recovery: build into a fresh collection and "
            f"switch through promote_replacement — --replacement <new_name> (verified switch "
            f"over {collection!r}), or --collection <new_name> to build only — see "
            "'Failed-rebuild recovery' in this module docstring."
        )

    # 3) Counts (read-only).
    logger.info("Fetching existing chunks from Postgres...")
    async with session_factory() as session:
        query = text(
            """
            SELECT c.id, c.workspace_id, dv.document_id, c.document_version_id,
                   c.text, c.page, c.section, c.language, c.chunk_metadata
            FROM chunks c
            JOIN document_versions dv ON c.document_version_id = dv.id
            JOIN documents d ON dv.document_id = d.id
            WHERE d.deleted_at IS NULL
            """
        )
        rows = (await session.execute(query)).all()

    n_batches = math.ceil(len(rows) / batch_size) if rows else 0
    logger.info(
        "counts: live_chunks=%d batches=%d batch_size=%d existing_collection_points=%d",
        len(rows),
        n_batches,
        batch_size,
        inspection["points"],
    )

    # Stores (the only write-capable objects) are created for real runs only.
    qdrant: QdrantVectorStore | None = None
    lexical_store: PostgresLexicalStore | None = None
    if not dry_run:
        lexical_store = PostgresLexicalStore(session_factory=session_factory)
        qdrant = QdrantVectorStore(collection=target)
        await qdrant.ensure_collection(collection=target, dimension=observed_dim)

    total_indexed = 0
    total_validated = 0
    try:
        for i in range(0, len(rows), batch_size):
            batch_rows = rows[i : i + batch_size]
            texts = [r.text for r in batch_rows]

            logger.info("Generating embeddings for batch %d-%d...", i, i + len(batch_rows))
            vectors = await embed_provider.embed_documents(texts)
            if len(vectors) != len(batch_rows):
                raise RuntimeError(
                    f"Embedding count mismatch: expected {len(batch_rows)}, got {len(vectors)}"
                )

            batch_records: list[VectorRecord] = []
            for r, vec in zip(batch_rows, vectors, strict=True):
                validate_vector(vec, observed_dim)
                total_validated += 1
                if not dry_run:
                    batch_records.append(build_record(r, vec, identity))

            if dry_run:
                logger.info(
                    "[DRY RUN] would index batch %d-%d (%d records, 0 writes)",
                    i,
                    i + len(batch_rows),
                    len(batch_rows),
                )
            else:
                if lexical_store is None or qdrant is None:
                    raise RuntimeError("stores were not initialised for a real run")
                await lexical_store.index(records=batch_records)
                await qdrant.upsert(collection=target, records=batch_records)
                logger.info("Indexed batch %d to %d", i, i + len(batch_records))

            total_indexed += len(batch_rows)
        # T-V1-045 — build-then-switch, the only collection switch this script
        # performs. promote_replacement verifies the replacement read-only
        # (exists / dense dims / >= expected_points) and only switches after
        # that passes, so a short or half-built replacement aborts here with
        # the live collection still serving; nothing else in this file deletes
        # or aliases a collection.
        if not dry_run and replacement is not None:
            if qdrant is None:
                raise RuntimeError("stores were not initialised for a real run")
            logger.info(
                "promote_replacement(live=%r, replacement=%r, expected_points=%d) — "
                "verify first, switch only if it passes",
                collection,
                replacement,
                total_indexed,
            )
            if total_indexed == 0:
                logger.warning(
                    "expected_points=0 — verification covers existence and dimensions only "
                    "(no chunks to count) before switching %r",
                    collection,
                )
            await qdrant.promote_replacement(
                live=collection, replacement=replacement, expected_points=total_indexed
            )
            logger.info("switch complete: %r now serves %r", collection, replacement)
    finally:
        if qdrant is not None:
            await qdrant.close()

    if dry_run:
        if replacement is not None:
            if action == "refuse":
                # Honest dry-run: the real run would raise ReindexRefused here,
                # so no switch is on offer and the live collection keeps serving.
                logger.info(
                    "[DRY RUN] switch plan: BLOCKED — the run would refuse to build %r "
                    "(%s); promote_replacement is NOT called, live=%r keeps serving",
                    replacement,
                    reason,
                    collection,
                )
            else:
                logger.info(
                    "[DRY RUN] switch plan: promote_replacement(live=%r, replacement=%r, "
                    "expected_points=%d) — verified read-only first, then the alias switch; "
                    "switch_writes=0",
                    collection,
                    replacement,
                    total_indexed,
                )
        logger.info(
            "[DRY RUN] done: provider=%s model=%s configured_dim=%d observed_dim=%d nonzero=%d "
            "live_chunks=%d validated=%d existing_points=%d action=%s WRITES=0 "
            "(fts=0 qdrant=0 collections_created=0 aliases_changed=0)",
            type(embed_provider).__name__,
            identity["embedding_model"],
            embed_provider.dimension,
            observed_dim,
            nonzero_dim,
            len(rows),
            total_validated,
            inspection["points"],
            action,
        )
        return 0

    logger.info("Reindexing complete! Total chunks indexed: %d", total_indexed)
    return total_indexed


async def main() -> None:
    parser = argparse.ArgumentParser(description="Reindex all chunks into FTS and Qdrant")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report provider/model/dims/counts by embedding every batch through the real "
        "provider, with ZERO writes (no collection create, no FTS insert, no Qdrant upsert)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=100, help="Batch size for embedding and indexing"
    )
    parser.add_argument(
        "--collection", type=str, default="knowledge", help="Qdrant collection name"
    )
    parser.add_argument(
        "--replacement",
        type=str,
        default=None,
        help="Build-then-switch recovery (T-V1-045): build everything into this collection "
        "instead of --collection, then promote it over --collection with "
        "promote_replacement (T-V1-015: read-only verification first, alias switch only "
        "after it passes). Without this flag no switch happens at all",
    )
    args = parser.parse_args()

    try:
        await reindex(
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            collection=args.collection,
            replacement=args.replacement,
        )
    except ReindexRefused as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc
    except VectorStoreError as exc:
        # promote_replacement's message already states whether the live
        # collection is untouched; exit 3 keeps "refused" (2) and "switch/build
        # failed at the store" distinguishable for callers.
        logger.error("store error: %s", exc)
        raise SystemExit(3) from exc


if __name__ == "__main__":
    asyncio.run(main())
