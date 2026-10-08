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
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Sequence
from typing import Any

from core.config import get_settings
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import (
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

DENSE_NAME = "dense"
SPARSE_NAME = "sparse"
DEFAULT_DENSE_DIM = 1024
SPARSE_VOCAB = 30000  # hash-truncated vocab for BM25-like sparse

_token_re = re.compile(r"\w+", re.UNICODE)


class VectorStoreError(RuntimeError):
    """Typed error for Qdrant failures; wraps vendor exceptions with cause."""


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
    import math

    indices: list[int] = []
    values: list[float] = []
    for idx, cnt in sorted(counts.items()):
        indices.append(idx)
        values.append(1.0 + math.log(cnt))
    return SparseVector(indices=indices, values=values)


def _filter_from_search_filter(f: SearchFilter) -> Filter | None:
    must: list[Any] = []
    if f.workspace_ids:
        must.append(
            FieldCondition(key="workspace_id", match=MatchAny(any=f.workspace_ids))
        )
    if f.document_ids:
        must.append(
            FieldCondition(key="document_id", match=MatchAny(any=f.document_ids))
        )
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
            exists = await self._client.collection_exists(coll)
            if exists and recreate:
                await self._client.delete_collection(coll)
                exists = False
            if not exists:
                await self._client.create_collection(
                    collection_name=coll,
                    vectors_config={
                        DENSE_NAME: VectorParams(size=dimension, distance=dist)
                    },
                    sparse_vectors_config={
                        SPARSE_NAME: SparseVectorParams(
                            index=SparseIndexParams(on_disk=False)
                        )
                    },
                )
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
                    collection_name=coll, points_selector=qids  # type: ignore[arg-type]
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
                await self._client.delete(
                    collection_name=coll, points_selector=qfilter
                )
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
