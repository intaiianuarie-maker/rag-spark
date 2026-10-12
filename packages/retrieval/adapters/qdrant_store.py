"""Qdrant hybrid vector store — dense + sparse (BM25) named vectors.

Reuse: Qdrant Apache-2.0 + qdrant-client Apache-2.0 (see REUSE_MAP.md,
NOTICES.md). Adapter speaks Qdrant HTTP via qdrant-client; no Qdrant type
crosses the VectorStore ABC boundary — we return plain RetrievedItem.

Design: one collection with two named vectors
  - "dense"  : bge-m3 1024-d, Cosine (served by infinity)
  - "sparse" : BM25-like SparseVector (hash-truncated vocab)

SearchFilter → payload Filter (workspace_ids, document_ids, language,
metadata) applied *before* ranking (R-004). Sparse query vector is derived
from the same text→sparse helper used at index time, so hybrid is deterministic.

Storage boundary (T-V1-015, RP-10/QF-10)
-----------------------------------------
This module is the **single** vector-write boundary: the ingest pipeline and
``scripts/reindex_all_chunks.py`` both reach Qdrant through ``upsert``, so
validation lives here once instead of being re-implemented per caller.

* ``upsert`` validates every record — dimension, NaN/Inf, all-zero — *before*
  a single ``PointStruct`` is built and before any client call. One bad record
  in a batch ⇒ zero writes for that batch, raised as
  ``VectorStoreInvalidVector`` with a stable ``reason``.
* The expected dimension is the collection's own configured size: recorded by
  ``ensure_collection``, otherwise read (read-only) and cached. If it cannot be
  established the write is refused — the boundary fails closed rather than
  writing unverified.
* A rebuild must never take the live index down with it. ``ensure_collection``
  with ``recreate=True`` refuses to delete a collection that still holds points,
  and ``promote_replacement`` is the only sanctioned switch: it verifies the
  replacement first and performs no destructive call when verification fails,
  so a failed or interrupted rebuild leaves the prior index serving.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from collections.abc import Sequence
from typing import Any

from core.config import get_settings
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import (
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from ..interfaces import RetrievalMethod, RetrievedItem, SearchFilter, VectorRecord, VectorStore
from .infinity_embed import _is_zero_vector

DENSE_NAME = "dense"
SPARSE_NAME = "sparse"
DEFAULT_DENSE_DIM = 1024
SPARSE_VOCAB = 30000  # hash-truncated vocab for BM25-like sparse

_token_re = re.compile(r"\w+", re.UNICODE)


class VectorStoreError(RuntimeError):
    """Typed error for Qdrant failures; wraps vendor exceptions with cause."""


#: Rejection reasons emitted by the storage boundary (T-V1-015). Stable strings:
#: callers/tests key off them, not off message text.
REJECT_DIMENSION = "dimension_mismatch"
REJECT_NON_FINITE = "non_finite"
REJECT_ZERO = "zero_vector"


class VectorStoreInvalidVector(VectorStoreError):
    """A record failed storage-boundary validation (T-V1-015).

    Raised *before* any client call, so a batch containing an invalid vector
    results in zero writes rather than a partial or poisoned index.
    ``reason`` is one of the ``REJECT_*`` constants and ``record_id`` names the
    offending record.
    """

    def __init__(self, reason: str, message: str, *, record_id: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.record_id = record_id


def validate_record_vector(record: VectorRecord, *, expected_dim: int) -> None:
    """Storage boundary (T-V1-015): reject a bad vector before any write.

    Checks, in order: dimension, finiteness (NaN/Inf), all-zero. Reuses the
    embedding provider's zero-vector tolerance so there is a single definition
    of "manufactured vector" across provider, pipeline and store.
    """
    vector = record.vector
    if len(vector) != expected_dim:
        raise VectorStoreInvalidVector(
            REJECT_DIMENSION,
            f"record {record.id}: vector dimension {len(vector)} != collection "
            f"dimension {expected_dim} — refusing to write",
            record_id=record.id,
        )
    for position, value in enumerate(vector):
        if not math.isfinite(value):
            raise VectorStoreInvalidVector(
                REJECT_NON_FINITE,
                f"record {record.id}: vector[{position}] is {value!r} (NaN/Inf) — "
                "refusing to write a non-finite embedding",
                record_id=record.id,
            )
    if _is_zero_vector(vector):
        raise VectorStoreInvalidVector(
            REJECT_ZERO,
            f"record {record.id}: all-zero vector (no semantic signal) — "
            "refusing to write a manufactured embedding",
            record_id=record.id,
        )


def validate_records(records: Sequence[VectorRecord], *, expected_dim: int) -> None:
    """Validate a whole batch before a single point is built or sent."""
    for record in records:
        validate_record_vector(record, expected_dim=expected_dim)


def _to_point_id(s: str) -> str:
    """Qdrant point id must be UUID or uint; map arbitrary string ids deterministically."""
    try:
        uuid.UUID(s)
        return s
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, s))


def _text_to_sparse(text: str) -> SparseVector:
    """Deterministic BM25-like sparse vector from text (hash-truncated vocab)."""
    counts: dict[int, float] = {}
    for tok in _token_re.findall(text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16) % SPARSE_VOCAB
        counts[h] = counts.get(h, 0.0) + 1.0

    indices: list[int] = []
    values: list[float] = []
    for idx, cnt in sorted(counts.items()):
        indices.append(idx)
        values.append(1.0 + math.log(cnt))
    return SparseVector(indices=indices, values=values)


def _filter_from_search_filter(f: SearchFilter) -> Filter | None:
    must: list[Any] = []
    if f.workspace_ids:
        must.append(FieldCondition(key="workspace_id", match=MatchAny(any=f.workspace_ids)))
    if f.document_ids:
        must.append(FieldCondition(key="document_id", match=MatchAny(any=f.document_ids)))
    if f.language:
        must.append(FieldCondition(key="language", match=MatchValue(value=f.language)))
    for k, v in f.metadata.items():
        must.append(FieldCondition(key=f"metadata.{k}", match=MatchValue(value=v)))
    if not must:
        return None
    return Filter(must=must)


class QdrantVectorStore(VectorStore):
    """Qdrant hybrid store — implements retrieval.interfaces.VectorStore.

    Thin adapter: Qdrant does storage + ANN + filtering; we do mapping and
    preserve provenance (document/chunk/page/section) for citations.
    No Qdrant type leaves this module.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        collection: str | None = None,
        timeout: int = 10,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        settings = get_settings()
        qurl = url or os.environ.get("QDRANT_URL") or settings.qdrant_url
        self._client = client or AsyncQdrantClient(url=qurl, timeout=timeout)
        self._default_collection = collection
        #: Collection → configured dense dimension, learned once (T-V1-015).
        self._dimensions: dict[str, int] = {}

    @staticmethod
    def _dense_dim(info: Any) -> int | None:
        """Read the configured dense dimension out of a ``get_collection`` result."""
        try:
            vectors = info.config.params.vectors
        except AttributeError:
            return None
        try:
            if isinstance(vectors, dict):
                entry = vectors.get(DENSE_NAME)
                if entry is None and len(vectors) == 1:
                    entry = next(iter(vectors.values()))
                return int(entry.size)  # type: ignore[union-attr]
            return int(vectors.size)
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    async def _expected_dimension(self, coll: str) -> int:
        """Collection dimension for the write boundary; never guess (T-V1-015).

        Cached after ``ensure_collection`` or the first read. When it cannot be
        established the caller gets a ``VectorStoreError`` and **no write is
        attempted** — refusing is the safe outcome.
        """
        cached = self._dimensions.get(coll)
        if cached:
            return cached
        try:
            info = await self._client.get_collection(coll)
        except Exception as e:
            raise VectorStoreError(
                f"cannot determine the dimension of {coll!r} ({type(e).__name__}: {e}) — "
                "refusing to write vectors that cannot be verified against the collection"
            ) from e
        dim = self._dense_dim(info)
        if not dim or dim <= 0:
            raise VectorStoreError(
                f"collection {coll!r} reports no usable dense dimension — "
                "refusing to write unverified vectors"
            )
        self._dimensions[coll] = dim
        return dim

    async def _point_count(self, coll: str) -> int:
        """Read-only point count (used to protect a populated index from deletion)."""
        try:
            info = await self._client.get_collection(coll)
        except Exception:
            return -1  # unknown ⇒ treat as populated: the safe direction
        try:
            return int(info.points_count or 0)
        except (AttributeError, TypeError, ValueError):
            return -1

    async def ensure_collection(
        self,
        *,
        collection: str,
        dimension: int,
        distance: str = "cosine",
        recreate: bool = False,
    ) -> None:
        try:
            dist = {
                "cosine": Distance.COSINE,
                "euclid": Distance.EUCLID,
                "dot": Distance.DOT,
            }.get(distance.lower(), Distance.COSINE)
            coll = collection or self._default_collection
            if not coll:
                raise VectorStoreError("collection name required")
            if dimension <= 0:
                raise VectorStoreError(f"dimension must be positive, got {dimension}")
            exists = await self._client.collection_exists(coll)
            if exists and recreate:
                # T-V1-015: never destroy a usable index to start a rebuild.
                # An empty/absent target may be recreated freely; a populated
                # one is refused so a failed rebuild cannot take the prior
                # index with it (build a replacement, then promote it).
                points = await self._point_count(coll)
                if points != 0:
                    raise VectorStoreError(
                        f"refusing recreate=True on {coll!r}: it holds {points} points and a "
                        "failed or interrupted rebuild must not take the live index with it. "
                        "Build a replacement collection (ensure_collection on a fresh name, "
                        "upsert into it), verify it, then promote_replacement()."
                    )
                await self._client.delete_collection(coll)
                exists = False
            if not exists:
                await self._client.create_collection(
                    collection_name=coll,
                    vectors_config={DENSE_NAME: VectorParams(size=dimension, distance=dist)},
                    sparse_vectors_config={
                        SPARSE_NAME: SparseVectorParams(index=SparseIndexParams(on_disk=False))
                    },
                )
                self._dimensions[coll] = dimension
            else:
                info = await self._client.get_collection(coll)
                try:
                    vectors = info.config.params.vectors
                    if isinstance(vectors, dict):
                        vec_size = vectors[DENSE_NAME].size
                    else:
                        vec_size = vectors.size  # type: ignore[union-attr]
                    if vec_size != dimension:
                        raise VectorStoreError(
                            f"collection {coll} dimension mismatch: {vec_size} != {dimension}"
                        )
                except (AttributeError, KeyError, TypeError):
                    pass
                else:
                    self._dimensions[coll] = dimension
        except VectorStoreError:
            raise
        except Exception as e:
            raise VectorStoreError(f"ensure_collection failed for {collection}: {e}") from e

    async def upsert(self, *, collection: str, records: Sequence[VectorRecord]) -> int:
        if not records:
            return 0
        coll = collection or self._default_collection
        if not coll:
            raise VectorStoreError("collection name required")
        try:
            # T-V1-015 boundary: the whole batch is validated before the first
            # point is built, so an invalid record means ZERO writes — not a
            # partial batch and not a poisoned index.
            expected_dim = await self._expected_dimension(coll)
            validate_records(records, expected_dim=expected_dim)
            points: list[PointStruct] = []
            for r in records:
                payload: dict[str, Any] = {
                    "workspace_id": r.workspace_id,
                    "content": r.content,
                    "metadata": r.metadata or {},
                }
                if r.document_id is not None:
                    payload["document_id"] = r.document_id
                if r.document_version_id is not None:
                    payload["document_version_id"] = r.document_version_id
                if r.chunk_id is not None:
                    payload["chunk_id"] = r.chunk_id
                if r.page is not None:
                    payload["page"] = r.page
                if r.section is not None:
                    payload["section"] = r.section
                if "language" in r.metadata:
                    payload["language"] = r.metadata["language"]
                sparse = _text_to_sparse(r.content)
                points.append(
                    PointStruct(
                        id=_to_point_id(r.id),
                        vector={DENSE_NAME: r.vector, SPARSE_NAME: sparse},
                        payload=payload,
                    )
                )
            await self._client.upsert(collection_name=coll, points=points)
            return len(points)
        except VectorStoreError:
            raise
        except Exception as e:
            raise VectorStoreError(f"upsert failed for {collection}: {e}") from e

    async def promote_replacement(
        self,
        *,
        live: str,
        replacement: str,
        expected_points: int = 0,
    ) -> None:
        """Switch ``live`` over to a fully built ``replacement`` (T-V1-015).

        The second half of *build replacement, then switch*. **Verification
        comes first and is entirely read-only**: the replacement must exist,
        report the same dense dimension as the live index, and hold at least
        ``expected_points`` points. Any verification failure raises and
        performs no destructive call at all, so a failed or interrupted
        rebuild leaves the prior index serving exactly as before.

        Only after verification does the switch run: if ``live`` is a real
        collection it is deleted and the name is immediately re-issued as an
        alias to ``replacement`` (Qdrant has no rename, so the old collection
        can only retire once the replacement is proven); if ``live`` is
        already an alias it is repointed atomically in a single alias request
        and the superseded physical collection is left in place for the
        operator to drop. The prior index is never deleted before this point.
        """
        if not live or not replacement:
            raise VectorStoreError("live and replacement collection names are required")
        if live == replacement:
            raise VectorStoreError("live and replacement must be different collections")

        # --- verification phase: read-only, zero destructive calls ---------
        if not await self._client.collection_exists(replacement):
            raise VectorStoreError(
                f"replacement {replacement!r} does not exist — nothing promoted; "
                f"{live!r} is untouched and still serving"
            )
        replacement_info = await self._client.get_collection(replacement)
        replacement_dim = self._dense_dim(replacement_info)
        if not replacement_dim or replacement_dim <= 0:
            raise VectorStoreError(
                f"replacement {replacement!r} reports no usable dense dimension — "
                f"nothing promoted; {live!r} is untouched"
            )
        replacement_points = await self._point_count(replacement)
        if replacement_points < expected_points:
            raise VectorStoreError(
                f"replacement {replacement!r} is incomplete: {replacement_points} of "
                f"{expected_points} expected points — nothing promoted; {live!r} is "
                "untouched. Delete the half-built replacement and re-run the rebuild; "
                "never finish it by writing into the live index."
            )
        live_is_collection = await self._client.collection_exists(live)
        if live_is_collection:
            live_dim = self._dense_dim(await self._client.get_collection(live))
            if live_dim and live_dim != replacement_dim:
                raise VectorStoreError(
                    f"dimension mismatch: {live!r} is {live_dim}-d but {replacement!r} is "
                    f"{replacement_dim}-d — nothing promoted; {live!r} is untouched"
                )

        # --- switch phase: only reachable with a verified replacement ------
        try:
            if live_is_collection:
                await self._client.delete_collection(live)
                operations: list[Any] = [
                    CreateAliasOperation(
                        create_alias=CreateAlias(collection_name=replacement, alias_name=live)
                    )
                ]
                await self._client.update_collection_aliases(change_aliases_operations=operations)
            else:
                aliases = await self._client.get_aliases()
                existing = [
                    a.alias_name
                    for a in getattr(aliases, "aliases", []) or []
                    if a.alias_name == live
                ]
                operations = [
                    CreateAliasOperation(
                        create_alias=CreateAlias(collection_name=replacement, alias_name=live)
                    )
                ]
                if existing:
                    operations.insert(
                        0, DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=live))
                    )
                await self._client.update_collection_aliases(change_aliases_operations=operations)
        except VectorStoreError:
            raise
        except Exception as e:
            raise VectorStoreError(
                f"switch to {replacement!r} failed after verification ({type(e).__name__}: "
                f"{e}) — inspect {live!r}/{replacement!r} before retrying"
            ) from e
        self._dimensions[live] = replacement_dim
        self._dimensions[replacement] = replacement_dim

    async def count_ids(self, *, collection: str, ids: Sequence[str]) -> int:
        """Count how many of ``ids`` currently exist as points in the collection.

        Write-verification seam (T-V1-042): the ingest pipeline calls this
        after ``upsert`` to prove the points really landed, so it is
        read-only and cheap — one retrieve for the whole id batch, no scroll
        and no delete. Ids are deduplicated and mapped through the same
        deterministic ``_to_point_id`` used at upsert time, with semantics
        identical to ``PostgresLexicalStore.count_ids``: the number of
        distinct given ids present in the store right now. A transport
        failure raises ``VectorStoreError`` instead of falling back to a
        guessed count, so the pipeline reports STORE_WRITE_UNVERIFIED rather
        than a false success.
        """
        if not ids:
            return 0
        coll = collection or self._default_collection
        if not coll:
            raise VectorStoreError("collection name required")
        try:
            qids = list(dict.fromkeys(_to_point_id(i) for i in ids))
            existing = await self._client.retrieve(
                collection_name=coll, ids=qids, with_payload=False, with_vectors=False
            )
            return len(existing)
        except VectorStoreError:
            raise
        except Exception as e:
            raise VectorStoreError(f"count_ids failed for {collection}: {e}") from e

    async def search(
        self,
        *,
        collection: str,
        vector: list[float],
        top_k: int,
        query_filter: SearchFilter,
    ) -> list[RetrievedItem]:
        if top_k <= 0:
            return []
        if not query_filter.workspace_ids:
            return []
        coll = collection or self._default_collection
        if not coll:
            raise VectorStoreError("collection name required")
        try:
            qfilter = _filter_from_search_filter(query_filter)
            resp = await self._client.query_points(
                collection_name=coll,
                query=vector,
                using=DENSE_NAME,
                query_filter=qfilter,
                limit=top_k,
                with_payload=True,
            )
            results = resp.points
            items: list[RetrievedItem] = []
            for r in results:
                p = r.payload or {}
                items.append(
                    RetrievedItem(
                        content=str(p.get("content", "")),
                        score=float(r.score),
                        method=RetrievalMethod.DENSE,
                        workspace_id=p.get("workspace_id"),
                        document_id=p.get("document_id"),
                        document_version_id=p.get("document_version_id"),
                        chunk_id=p.get("chunk_id"),
                        page=p.get("page"),
                        section=p.get("section"),
                        scores={"dense": float(r.score)},
                        metadata=dict(p.get("metadata", {})),
                    )
                )
            return items
        except VectorStoreError:
            raise
        except Exception as e:
            raise VectorStoreError(f"search failed for {collection}: {e}") from e

    async def delete(
        self,
        *,
        collection: str,
        ids: Sequence[str] | None = None,
        query_filter: SearchFilter | None = None,
    ) -> int:
        coll = collection or self._default_collection
        if not coll:
            raise VectorStoreError("collection name required")
        try:
            if ids:
                qids = [_to_point_id(i) for i in ids]
                # Honest count: check how many of the ids actually exist
                try:
                    existing = await self._client.retrieve(
                        collection_name=coll, ids=qids, with_payload=False, with_vectors=False
                    )
                    count = len(existing)
                except Exception:
                    count = len(ids)
                await self._client.delete(
                    collection_name=coll,
                    points_selector=qids,  # type: ignore[arg-type]
                )
                return count
            if query_filter is not None:
                qfilter = _filter_from_search_filter(query_filter)
                if qfilter is None:
                    return 0
                # Count before delete for honest return
                try:
                    scrolled, _ = await self._client.scroll(
                        collection_name=coll,
                        scroll_filter=qfilter,
                        limit=10000,
                        with_payload=False,
                        with_vectors=False,
                    )
                    count = len(scrolled)
                except Exception:
                    count = 1
                await self._client.delete(collection_name=coll, points_selector=qfilter)
                return count
            return 0
        except VectorStoreError:
            raise
        except Exception as e:
            raise VectorStoreError(f"delete failed for {collection}: {e}") from e

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


def create_vector_store(**kwargs: Any) -> VectorStore:
    """Harness factory — frozen M0-04 contract (tests/README.md).

    Returns a VectorStore backed by Qdrant (qdrant-client, Apache-2.0).
    Env: QDRANT_URL (default http://localhost:6333).
    """
    return QdrantVectorStore(**kwargs)
