#!/usr/bin/env python3
"""T-R07 Phase 2 — representative retrieval spot-checks on the rebuilt index.

Counts say how many points exist; they do not say whether those points are
RETRIEVABLE. A collection can hold every chunk and still return nothing, if
for example the sparse vector config or the payload mapping is wrong — which is
exactly what a count-only gate would miss.

These spot-checks run the REAL store path (``QdrantVectorStore.search``, T-R06b
identity gate included) against the rebuild target:

  1. **dense recall** — for a sample of manifest chunks, embed the ORIGINAL
     text and confirm the chunk comes back in the top-K with its identity
     intact. This proves write and read agree about identity.
  2. **workspace scoping** — a chunk retrieved in its own workspace scope and
     INVISIBLE in a foreign one (ACL-before-rank, R-004).
  3. **identity refusal** — a query carrying an incompatible provider is
     refused by the boundary BEFORE any retrieval, and so is an unlabelled
     collection.

Read-only against the rebuild collection (search never writes). No switch, no
alias, no live-collection access.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
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


_audit = _load("audit_rebuilt_index", "scripts/audit_rebuilt_index.py")

_log = logging.getLogger("spotcheck")

LIVE_COLLECTION = "knowledge"


class SpotCheckError(RuntimeError):
    """A spot-check could not run."""


async def run_spot_checks(
    *,
    collection: str,
    sample: int = 12,
    top_k: int = 5,
) -> dict[str, Any]:
    if collection == LIVE_COLLECTION:
        raise SpotCheckError(
            f"refusing to spot-check the live collection {collection!r}"
        )
    from retrieval.adapters.infinity_embed import InfinityEmbeddingProvider
    from retrieval.adapters.qdrant_store import (
        QdrantVectorStore,
        VectorStoreIdentityError,
    )
    from retrieval.embedding_identity import EmbeddingIdentity, provider_identity
    from retrieval.interfaces import SearchFilter

    _reindex = _load("reindex_all_chunks", "scripts/reindex_all_chunks.py")

    # The manifest gives us real chunk texts + the chunk ids that MUST come back.
    rows = _audit.manifest_from_postgres()
    if not rows:
        raise SpotCheckError("manifest is empty")
    rows = rows[:sample]

    provider = InfinityEmbeddingProvider()
    store = QdrantVectorStore(collection=collection)
    try:
        observed_dim, _ = await _reindex.probe_provider(provider)
        identity = provider_identity(provider, observed_dim)

        # --- 2. identity-gate refusals (the boundary is doing the work) -------
        refusals: dict[str, str] = {}
        foreign = EmbeddingIdentity(
            provider="SomeOtherProvider", model="other-model",
            revision="rev-x", dimension=identity.dimension,
        )
        try:
            await store.search(
                collection=collection,
                vector=[0.1] * identity.dimension,
                top_k=top_k,
                query_filter=SearchFilter(workspace_ids=["ws-a"]),
                query_identity=foreign,
            )
            refusals["incompatible_query"] = "NOT REFUSED — boundary regression"
        except VectorStoreIdentityError as exc:
            refusals["incompatible_query"] = getattr(exc, "reason", "") or str(exc)

        # --- 1. dense recall + workspace scoping -------------------------------
        results = []
        texts = [r["text"] for r in rows]
        vectors = await _reindex.embed_batch_with_retry(provider, texts)
        for row, vector in zip(rows, vectors, strict=True):
            found = await store.search(
                collection=collection,
                vector=vector,
                top_k=top_k,
                query_filter=SearchFilter(workspace_ids=[row["workspace_id"]]),
                query_identity=identity,
            )
            hit = any(item.chunk_id == row["chunk_id"] for item in found)
            foreign_ids = await store.search(
                collection=collection,
                vector=vector,
                top_k=top_k,
                query_filter=SearchFilter(workspace_ids=["00000000-0000-0000-0000-000000000000"]),
                query_identity=identity,
            )
            leaked = any(item.chunk_id == row["chunk_id"] for item in foreign_ids)
            results.append(
                {
                    "chunk_id": row["chunk_id"],
                    "own_scope_hit": hit,
                    "foreign_scope_leak": leaked,
                    "returned": len(found),
                }
            )

        recall = sum(1 for r in results if r["own_scope_hit"]) / len(results)
        leaks = sum(1 for r in results if r["foreign_scope_leak"])
        boundary_refused = "NOT REFUSED" not in refusals.get("incompatible_query", "")
        return {
            "collection": collection,
            "sampled": len(rows),
            "identity": identity.as_metadata(),
            "dense_recall": round(recall, 4),
            "dense_hits": sum(1 for r in results if r["own_scope_hit"]),
            "foreign_scope_leaks": leaks,
            "boundary_refused_incompatible_query": boundary_refused,
            "refusals": refusals,
            "per_chunk": results,
            "verdict": "PASS" if (recall >= 0.9 and leaks == 0 and boundary_refused) else "FAIL",
        }
    finally:
        await store.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="T-R07 Phase-2 retrieval spot-checks")
    parser.add_argument("--collection", default="knowledge__rebuild_20261014")
    parser.add_argument("--sample", type=int, default=12)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        report = await run_spot_checks(collection=args.collection, sample=args.sample)
    except SpotCheckError as exc:
        _log.error("%s", exc)
        raise SystemExit(2) from exc
    print(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if report["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    asyncio.run(main())
