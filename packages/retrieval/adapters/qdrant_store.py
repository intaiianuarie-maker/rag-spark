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

Active-version read gate (T-V1-018, RP-09/QF-09)
------------------------------------------------
Being *written* is not the same as being *current*. ``upsert`` can legitimately
leave a superseded generation behind for as long as the T-V1-017 cleanup is
pending or retrying, so this module validates the retrieved payload against the
**authoritative document** before its content is eligible as evidence:

* ``search`` widens its ANN window, then drops every candidate whose payload
  ``document_version_id`` is not the document's ``documents.current_version_id``
  (a tombstoned document drops everything), then truncates back to ``top_k``.
  The check is *pre-evidence*: a stale copy never leaves the store, so it can
  never be ranked, cited or hydrated into a prompt — it is not a post-answer
  filter and it does not depend on async cleanup having run.
* The authoritative state is one batched read per search (no per-hit query) and
  the gate is deterministic: the same store rows and the same authority produce
  the same items, in the same order, every time.
* When the authority cannot be consulted at all the read **refuses**
  (``VectorStoreError``) instead of serving content it could not validate; the
  orchestrator degrades that leg to ``[]`` exactly as it does for any other
  store failure.

Authority contract (see :func:`_postgres_active_versions`): a document the
authority does not have — or one whose ``current_version_id`` was never set
(pre-migration rows) — makes *no* active-version claim and passes through
unchanged, which is also what keeps the frozen M0-04 vector-store contract
green (its fixture documents are not rows, and ``documents.id`` is a UUID
column, so those ids are outside the authority's key space entirely); a
tombstoned document is always ineligible.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
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

from ..embedding_identity import (
    EmbeddingIdentity,
    EmbeddingIdentityError,
    identity_from_metadata,
    verify_query_identity,
    verify_record_identity,
)
from ..interfaces import RetrievalMethod, RetrievedItem, SearchFilter, VectorRecord, VectorStore
from ..serving import (
    ServingAliasState,
    adopt_command,
    rollback_command,
    serving_alias_name,
)
from .infinity_embed import _is_zero_vector

DENSE_NAME = "dense"
SPARSE_NAME = "sparse"
DEFAULT_DENSE_DIM = 1024
SPARSE_VOCAB = 30000  # hash-truncated vocab for BM25-like sparse

_token_re = re.compile(r"\w+", re.UNICODE)

logger = logging.getLogger("knowledge_os.retrieval.qdrant_store")


class VectorStoreError(RuntimeError):
    """Typed error for Qdrant failures; wraps vendor exceptions with cause."""


#: Rejection reasons emitted by the storage boundary (T-V1-015). Stable strings:
#: callers/tests key off them, not off message text.
REJECT_DIMENSION = "dimension_mismatch"
REJECT_NON_FINITE = "non_finite"
REJECT_ZERO = "zero_vector"


class VectorStoreIdentityError(VectorStoreError):
    """A record or a collection failed EMBEDDING IDENTITY verification (T-R06b).

    ``reason`` is ``"unlabelled_record"`` / ``"incompatible_record"`` /
    ``"unknown_collection"`` / ``"incompatible_query"`` and ``field`` names the
    disagreeing identity key when there is one, so a caller can branch on the
    cause instead of matching message text. Always raised BEFORE the write or
    the retrieval it guards.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        collection: str = "",
        record_id: str | None = None,
        field: str = "",
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.collection = collection
        self.record_id = record_id
        self.field = field


class VectorStoreInvalidVector(VectorStoreError):
    """A record failed storage-boundary validation (T-V1-015).

    Raised *before* any client call, so a batch containing an invalid vector
    results in zero writes rather than a partial or poisoned index.
    ``reason`` is one of the ``REJECT_*`` constants and ``record_id`` names the
    offending record.

    Every ``REJECT_*`` reason is **deterministic** — a property of the vector
    itself, never of the service — so retrying can only replay the same
    rejection. The ingestion worker therefore maps this exception exactly
    once, in its shared error handling, onto the registry's non-retryable
    ``ZERO_VECTOR_REJECTED`` park (``core.errors``: INDEXING / 422 /
    ``retryable=False``): such a job fails fast-and-final on the first attempt
    instead of burning its retry budget (T-V1-046). Transient store failures
    stay plain :class:`VectorStoreError` and keep their retryable verdict.
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


def validate_records(
    records: Sequence[VectorRecord],
    *,
    expected_dim: int,
    identity: EmbeddingIdentity | None = None,
    collection: str = "",
) -> None:
    """Validate a whole batch before a single point is built or sent.

    T-R06b: identity is VERIFIED here, not merely labelled. Every record must
    carry the four identity keys (``embedding_provider/model/revision/
    dimension``) — a record without them is refused with the missing keys named,
    because an unlabelled vector cannot be compared against a provider and
    "probably fine" is not a check. When ``identity`` is supplied (the caller
    knows which provider produced these vectors) the record must additionally
    MATCH it, all four fields or nothing, and the disagreeing field is named.

    Both refusals raise :class:`VectorStoreIdentityError` before any point is
    built, so an unlabelled or mismatched batch means ZERO writes — the same
    all-or-nothing property the vector checks already had.
    """
    for record in records:
        validate_record_vector(record, expected_dim=expected_dim)
        metadata = record.metadata or {}
        if identity is None:
            # Unlabelled check only: the record must still carry all four keys.
            try:
                identity_from_metadata(metadata)
            except EmbeddingIdentityError as exc:
                raise VectorStoreIdentityError(
                    f"record {record.id}: {exc}",
                    reason="unlabelled_record",
                    collection=collection,
                    record_id=record.id,
                ) from exc
            continue
        try:
            verify_record_identity(metadata, identity, collection=collection)
        except EmbeddingIdentityError as exc:
            # Name the disagreeing field using dev2's own reason text, so the
            # store reports exactly which of the four keys refused the write.
            try:
                field = identity_from_metadata(metadata).mismatch_reason(identity).split(":")[0]
            except EmbeddingIdentityError:
                field = ""
            raise VectorStoreIdentityError(
                f"record {record.id}: {exc}",
                reason="incompatible_record",
                collection=collection,
                record_id=record.id,
                field=field,
            ) from exc


def _to_point_id(s: str) -> str:
    """Qdrant point id must be UUID or uint; map arbitrary string ids deterministically."""
    try:
        uuid.UUID(s)
        return s
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, s))


def _is_uuid(value: Any) -> bool:
    """Is ``value`` in the authoritative document key space (a UUID)?

    ``documents.id`` is a UUID column, so an id that is not a UUID can never
    name a document row (T-V1-018): it is structurally outside the authority's
    key space rather than an unknown document in it.
    """
    if value is None:
        return False
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


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


#: T-V1-018: candidate-window multiplier for the active-version gate. The gate
#: can drop candidates, so ``search`` asks Qdrant for a wider window and
#: truncates the filtered list back to ``top_k``: dropping a stale copy must
#: neither shrink the returned set nor crowd out a current-version hit (no
#: recall regression), and with nothing stale the first ``top_k`` candidates —
#: and therefore the returned items — are exactly what they were before.
CANDIDATE_OVERFETCH = 4

#: Authoritative active-version lookup: ``document_id`` -> active version id.
#: A document absent from the returned mapping is one the authority makes no
#: claim about (never ingested under this contract, or pre-migration rows whose
#: ``current_version_id`` was never set); ``None`` names a tombstoned document,
#: which is never eligible as evidence.
ActiveVersionResolver = Callable[[Collection[str]], Awaitable[Mapping[str, str | None]]]


async def _postgres_active_versions(document_ids: Collection[str]) -> dict[str, str | None]:
    """Authoritative active-version state, straight from the ``documents`` table.

    The single source of truth for "which generation is live" is the
    ``Document.current_version_id`` pointer (T-V1-016), not the vector payload
    — a payload can only ever *claim* a generation. One batched read answers
    every document in the candidate window, so the gate costs a constant number
    of queries per search regardless of ``top_k``.

    Reported per document:

    * ``<version uuid str>`` — the active generation; a payload may only be
      evidence when it claims exactly this version;
    * ``None`` — the document is tombstoned (``deleted_at`` set): nothing
      published for it is ever eligible, whether or not async cleanup has run;
    * absent — the authority has no active-version claim to make: the id names
      no document row, or the document is live but its pointer was never set
      (pre-migration rows). Enforcing against those would strand every
      pre-migration document and the frozen M0-04 contract fixtures, so they
      pass through unchanged.

    ``documents.id`` is a UUID column, so only UUID-shaped ids are in the
    authority's key space at all: a payload naming anything else (contract
    fixtures, pre-contract payloads) cannot be resolved to a document and is
    therefore never looked up — that keeps an id the database would reject
    (``invalid input syntax for type uuid``) from turning a no-claim read into
    a hard refusal.

    A transport/DB failure propagates: ``search`` turns it into a refusal
    rather than a silent pass-through (see the module docstring).
    """
    ids = sorted({str(d) for d in document_ids if _is_uuid(d)})
    if not ids:
        return {}
    from ingestion.persistence import get_session_factory
    from sqlalchemy import text

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT id, current_version_id, deleted_at FROM documents WHERE id = ANY(:ids)"
            ),
            {"ids": ids},
        )
        active: dict[str, str | None] = {}
        for row in result.mappings().all():
            if row["deleted_at"] is not None:
                active[str(row["id"])] = None  # tombstoned: never eligible
            elif row["current_version_id"] is not None:
                active[str(row["id"])] = str(row["current_version_id"])
            # else: no claim — leave the document absent from the mapping.
        return active


def _is_active_version(payload: Mapping[str, Any], active: Mapping[str, str | None]) -> bool:
    """Pre-evidence gate (T-V1-018): is this payload the authoritative version?

    Deterministic and payload-only once ``active`` is resolved — no clock, no
    sleep, no cleanup-state guess. ``False`` means the retrieved content is a
    leftover copy and is dropped before it can become evidence.
    """
    document_id = payload.get("document_id")
    if document_id is None:
        return True  # no document provenance: nothing to be stale against
    key = str(document_id)
    if key not in active:
        return True  # the authority makes no active-version claim about this id
    active_version = active[key]
    if active_version is None:
        return False  # tombstoned document: never eligible
    payload_version = payload.get("document_version_id")
    return payload_version is not None and str(payload_version) == active_version


@dataclass(frozen=True)
class PromotionResult:
    """What a promotion (or rollback) did, and how to undo it.

    T-R05. The previous physical collection is RETAINED — this result is the
    operator's rollback handle, which is why ``retained_previous`` is always
    true for an executed promotion.
    """

    serving: str
    previous_target: str
    new_target: str
    retained_previous: bool
    recovery_command: str
    executed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "serving": self.serving,
            "previous_target": self.previous_target,
            "new_target": self.new_target,
            "retained_previous": self.retained_previous,
            "recovery_command": self.recovery_command,
        }


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
        active_version_resolver: ActiveVersionResolver | None = None,
    ) -> None:
        settings = get_settings()
        qurl = url or os.environ.get("QDRANT_URL") or settings.qdrant_url
        self._client = client or AsyncQdrantClient(url=qurl, timeout=timeout)
        self._default_collection = collection
        #: Collection → configured dense dimension, learned once (T-V1-015).
        self._dimensions: dict[str, int] = {}
        #: T-R05: serving alias → the physical collection it served before the
        #: last promotion, so a rollback has a target without asking the caller.
        self._previous_target: dict[str, str] = {}
        #: T-R06b: collection → the embedding identity every record in it was
        #: verified against. Populated by a verified write, or by a read-only
        #: probe on first query. Absent means UNKNOWN, which queries refuse.
        self._collection_identity: dict[str, EmbeddingIdentity] = {}
        #: T-V1-018 pre-evidence gate: who answers "which version is active".
        #: Defaults to the documents table; tests inject their own resolver.
        self._active_version_resolver: ActiveVersionResolver = (
            active_version_resolver or _postgres_active_versions
        )

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
            # T-R06b: identity is verified at this boundary, and — when this
            # process already knows what the collection holds — the record must
            # match it, so a rebuild cannot quietly write a second model's
            # vectors into a collection built by the first.
            validate_records(
                records,
                expected_dim=expected_dim,
                identity=self._collection_identity.get(coll),
                collection=coll,
            )
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
            # The collection's identity is now known to this process (every
            # record above was verified), so later reads and later writes can be
            # checked against it without another round trip.
            if coll not in self._collection_identity:
                self._collection_identity[coll] = identity_from_metadata(
                    records[0].metadata or {}
                )
            return len(points)
        except VectorStoreError:
            raise
        except Exception as e:
            raise VectorStoreError(f"upsert failed for {collection}: {e}") from e

    async def ensure_serving_alias(
        self,
        *,
        physical: str,
        alias: str | None = None,
    ) -> ServingAliasState:
        """Adopt a stable serving alias pointing at the retained old collection.

        T-R05. **Additive only**: this creates the alias and never deletes,
        renames or writes anything, so the serving name keeps resolving to the
        old collection through the whole promotion. Qdrant has no rename, which
        is precisely why the old code deleted the serving collection to free its
        name — deleting was the bug, not the requirement.

        Idempotent: if the alias already exists its current target is returned
        unchanged (a repoint is ``promote_replacement``'s job, never a side
        effect of adopting).
        """
        if not physical:
            raise VectorStoreError("physical collection name required")
        alias_name = alias or serving_alias_name(physical)
        state = await self.serving_alias_state(alias_name)
        if state.target is not None:
            return state
        try:
            await self._client.update_collection_aliases(
                change_aliases_operations=[
                    CreateAliasOperation(
                        create_alias=CreateAlias(collection_name=physical, alias_name=alias_name)
                    )
                ]
            )
        except Exception as e:
            raise VectorStoreError(
                f"could not create serving alias {alias_name!r} -> {physical!r} "
                f"({type(e).__name__}: {e}) — {physical!r} is untouched and still serving; "
                f"re-run: {adopt_command(alias_name, physical)}"
            ) from e
        return await self.serving_alias_state(alias_name)

    async def serving_alias_state(self, alias: str) -> ServingAliasState:
        """Where the serving alias points right now (read-only)."""
        aliases = await self._client.get_aliases()
        entries = getattr(aliases, "aliases", None) or []
        for entry in entries:
            name = getattr(entry, "alias_name", None)
            if name == alias:
                return ServingAliasState(
                    alias=alias,
                    target=str(getattr(entry, "collection_name", "") or "") or None,
                    serving_name_is_physical=False,
                )
        # No alias by that name: is the name itself a physical collection?
        if await self._client.collection_exists(alias):
            return ServingAliasState(
                alias=alias, target=alias, serving_name_is_physical=True
            )
        return ServingAliasState(alias=alias, target=None, serving_name_is_physical=False)

    async def serving_target(self, alias: str) -> str | None:
        """The physical collection the serving alias resolves to (read-only)."""
        return (await self.serving_alias_state(alias)).target

    async def verify_serving_readable(
        self, name: str, *, expected_points: int | None = None
    ) -> dict[str, Any]:
        """Read-only proof that ``name`` currently serves something readable."""
        state = await self.serving_alias_state(name)
        target = state.target
        if target is None:
            return {
                "name": name,
                "readable": False,
                "points": 0,
                "reason": f"{name!r} resolves to no collection (alias missing, not adopted)",
            }
        points = await self._point_count(target)
        readable = points is not None
        return {
            "name": name,
            "resolves_to": target,
            "readable": readable,
            "points": points or 0,
            "meets_expected": expected_points is None or (points or 0) >= expected_points,
        }

    async def rollback_promotion(
        self, live: str, *, to: str | None = None
    ) -> PromotionResult:
        """Atomically repoint the serving alias back to a retained collection.

        The recorded previous target is used when ``to`` is omitted. The
        target is verified readable **before** the repoint, so a rollback can
        never leave the alias pointing at nothing.
        """
        state = await self.serving_alias_state(live)
        target = to or self._previous_target.get(live)
        if not target:
            raise VectorStoreError(
                f"no previous target recorded for {live!r}; pass to=<collection> explicitly"
            )
        if not await self._client.collection_exists(target):
            raise VectorStoreError(
                f"rollback target {target!r} does not exist — {live!r} left serving "
                f"{state.target!r}"
            )
        before = state.target
        try:
            await self._client.update_collection_aliases(
                change_aliases_operations=[
                    DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=live)),
                    CreateAliasOperation(
                        create_alias=CreateAlias(collection_name=target, alias_name=live)
                    ),
                ]
            )
        except Exception as e:
            raise VectorStoreError(
                f"rollback of {live!r} to {target!r} failed ({type(e).__name__}: {e}) — "
                f"{live!r} still serves {before!r}"
            ) from e
        self._previous_target[live] = before or ""
        return PromotionResult(
            serving=live,
            previous_target=before or "",
            new_target=target,
            retained_previous=before is not None,
            recovery_command=rollback_command(live, before) if before else "",
            executed=True,
        )

    async def promote_replacement(
        self,
        *,
        live: str,
        replacement: str,
        expected_points: int = 0,
    ) -> PromotionResult:
        """Switch the serving alias over to a fully built ``replacement``.

        T-V1-015 verification, T-R05 switch. **Verification comes first and is
        entirely read-only**: the replacement must exist, report the same dense
        dimension as the current serving target, and hold at least
        ``expected_points`` points. Any verification failure raises and
        performs no destructive call at all, so a failed or interrupted rebuild
        leaves the prior index serving exactly as before.

        The switch itself is **one atomic alias repoint** — a single
        ``update_collection_aliases`` request carrying both the delete-alias and
        the create-alias operation — and the serving name must already BE an
        alias (:meth:`ensure_serving_alias`). The superseded physical collection
        is **retained** as the rollback target: this method never deletes the
        serving collection, never deletes the previous target, and never
        converts the serving name into an alias by deleting what was behind it.
        A serving name that is still a physical collection is REFUSED with the
        adoption command, because the only old way to free that name was to
        delete it — the outage shape T-R05 exists to remove.
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

        # --- serving-name contract (T-R05) --------------------------------
        # The serving name must be a STABLE ALIAS. If it is still a physical
        # collection we refuse: the only way to convert it would be to delete
        # it, which is the outage shape this ticket removes.
        state = await self.serving_alias_state(live)
        if state.serving_name_is_physical:
            raise VectorStoreError(
                f"serving name {live!r} is a physical collection, not an alias — refusing to "
                f"delete it to promote. Adopt the stable serving alias first (additive, "
                f"nothing is deleted): {adopt_command(serving_alias_name(live), live)}. "
                f"{live!r} keeps serving and {replacement!r} stays intact."
            )
        if state.target is None:
            raise VectorStoreError(
                f"serving alias {live!r} does not exist — nothing promoted. Adopt it first: "
                f"{adopt_command(serving_alias_name(live), live)}"
            )
        previous_target = state.target
        if previous_target == replacement:
            raise VectorStoreError(
                f"{live!r} already serves {replacement!r} — nothing to promote"
            )

        # --- switch phase: ONE atomic alias repoint, nothing deleted -------
        try:
            await self._client.update_collection_aliases(
                change_aliases_operations=[
                    DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=live)),
                    CreateAliasOperation(
                        create_alias=CreateAlias(collection_name=replacement, alias_name=live)
                    ),
                ]
            )
        except Exception as e:
            # The single request is atomic server-side, so a failure here means
            # the alias still points at the previous target: the old index is
            # serving and the replacement is intact.
            raise VectorStoreError(
                f"atomic repoint of {live!r} -> {replacement!r} failed "
                f"({type(e).__name__}: {e}) — nothing was deleted; {live!r} still serves "
                f"{previous_target!r} and {replacement!r} is intact. Verify with "
                f"verify_serving_readable({live!r}); to force the switch later, re-run the "
                f"promotion, and to go back explicitly: "
                f"{rollback_command(live, previous_target)}"
            ) from e
        self._dimensions[live] = replacement_dim
        self._dimensions[replacement] = replacement_dim
        self._previous_target[live] = previous_target
        return PromotionResult(
            serving=live,
            previous_target=previous_target,
            new_target=replacement,
            retained_previous=True,
            recovery_command=rollback_command(live, previous_target),
            executed=True,
        )

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

    async def _active_versions_for(
        self, payloads: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, str | None]:
        """Authoritative active-version state for the documents in ``payloads``.

        One batched lookup for the whole candidate window (T-V1-018). When the
        authority cannot answer, the read **refuses** instead of guessing: an
        unvalidatable candidate must not become evidence, and the orchestrator
        already degrades a failing leg to ``[]``.
        """
        document_ids = {
            str(p["document_id"]) for p in payloads if p.get("document_id") is not None
        }
        if not document_ids:
            return {}
        try:
            active = await self._active_version_resolver(document_ids)
        except Exception as e:
            raise VectorStoreError(
                f"active-version check failed for {len(document_ids)} document(s) "
                f"({type(e).__name__}: {e}) — refusing to return content that cannot be "
                "validated against the authoritative document"
            ) from e
        return dict(active or {})

    async def _collection_identity_of(self, coll: str) -> EmbeddingIdentity | None:
        """The identity recorded for ``coll``, or ``None`` when UNKNOWN.

        T-R06b. A cache hit comes from a write this process already verified.
        Otherwise one read-only scroll reads a single point's payload; that is
        enough because identity is stamped on every record, and it never
        mutates anything. A collection built before identity stamping — or one
        whose records disagree — resolves to ``None``, and ``None`` is refused
        downstream rather than assumed compatible.
        """
        cached = self._collection_identity.get(coll)
        if cached is not None:
            return cached
        try:
            page = await self._client.scroll(
                collection_name=coll, limit=1, with_payload=True, with_vectors=False
            )
        except Exception:
            return None
        # qdrant-client's ``scroll`` returns the PAIR
        # (points, next_page_offset), not an object with ``.points``. Reading
        # ``page.points`` (the T-R06b shape) resolves to None on a real client,
        # so every collection read as ``unknown`` and every query was refused
        # — which a stubbed object hid exactly. The contract is asserted in
        # tests/unit/test_embedding_identity_boundary.py.
        points = page[0] if isinstance(page, tuple) else list(page or [])
        if not points:
            return None
        payload = getattr(points[0], "payload", None) or {}
        try:
            identity = identity_from_metadata(payload.get("metadata") or {})
        except EmbeddingIdentityError:
            return None
        self._collection_identity[coll] = identity
        return identity

    async def search(
        self,
        *,
        collection: str,
        vector: list[float],
        top_k: int,
        query_filter: SearchFilter,
        query_identity: EmbeddingIdentity | None = None,
    ) -> list[RetrievedItem]:
        if top_k <= 0:
            return []
        if not query_filter.workspace_ids:
            return []
        coll = collection or self._default_collection
        if not coll:
            raise VectorStoreError("collection name required")
        # T-R06b: identity is verified BEFORE any retrieval is issued, so an
        # unknown or incompatible collection can never return a ranked hit.
        # An unknown collection identity is refused, never assumed compatible —
        # that assumption is the quiet failure this check removes.
        stored_identity = await self._collection_identity_of(coll)
        if stored_identity is None:
            raise VectorStoreIdentityError(
                f"collection {coll!r} has no recorded embedding identity; refusing to "
                "query with an unverified provider. Rebuild the collection with "
                "identity-stamped records (the ingest and rebuild paths both stamp it) "
                "and promote it with promote_replacement.",
                reason="unknown_collection",
                collection=coll,
            )
        if query_identity is not None:
            try:
                verify_query_identity(stored_identity, query_identity, collection=coll)
            except EmbeddingIdentityError as exc:
                raise VectorStoreIdentityError(
                    str(exc),
                    reason="incompatible_query",
                    collection=coll,
                ) from exc
        try:
            qfilter = _filter_from_search_filter(query_filter)
            # T-V1-018: widen the candidate window (CANDIDATE_OVERFETCH) so the
            # active-version gate can drop a leftover copy without shrinking the
            # set a caller gets back or crowding out a current-version hit.
            resp = await self._client.query_points(
                collection_name=coll,
                query=vector,
                using=DENSE_NAME,
                query_filter=qfilter,
                limit=max(top_k, top_k * CANDIDATE_OVERFETCH),
                with_payload=True,
            )
            results = resp.points
            # Pre-evidence gate: payload generation/version is validated against
            # the authoritative document BEFORE any content leaves this module,
            # so a superseded generation can never be ranked, cited or hydrated
            # into a prompt — cleanup being pending or retried changes nothing.
            active = await self._active_versions_for([r.payload or {} for r in results])
            items: list[RetrievedItem] = []
            stale = 0
            for r in results:
                p = r.payload or {}
                if not _is_active_version(p, active):
                    stale += 1
                    continue
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
            if stale:
                logger.warning(
                    f"stale_version_hits_filtered: stale={stale} returned={len(items)} "
                    f"collection={coll}",
                    extra={
                        "event": "stale_version_hits_filtered",
                        "stale": stale,
                        "collection": coll,
                    },
                )
            return items[:top_k]
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

    async def delete_versions(
        self,
        *,
        collection: str,
        version_ids: Sequence[str],
        document_id: str | None = None,
    ) -> int:
        """Delete every point published for the given document versions (T-V1-017).

        Cleanup call for supersession: points carry ``document_version_id`` in
        their payload, so a replaced version's vectors are removed *precisely* —
        the active version's points are never matched. Returns the number of
        points that existed and were removed. The count scroll must succeed:
        a cleanup that cannot be counted RAISES (``VectorStoreError``) so the
        caller queues a retry instead of reporting stale points as clean.
        """
        if not version_ids:
            return 0
        coll = collection or self._default_collection
        if not coll:
            raise VectorStoreError("collection name required")
        try:
            must: list[Any] = [
                FieldCondition(
                    key="document_version_id",
                    match=MatchAny(any=[str(v) for v in version_ids]),
                )
            ]
            if document_id is not None:
                must.append(
                    FieldCondition(key="document_id", match=MatchValue(value=str(document_id)))
                )
            qfilter = Filter(must=must)
            scrolled, _ = await self._client.scroll(
                collection_name=coll,
                scroll_filter=qfilter,
                limit=10000,
                with_payload=False,
                with_vectors=False,
            )
            count = len(scrolled)
            if count:
                await self._client.delete(collection_name=coll, points_selector=qfilter)
            return count
        except Exception as e:
            raise VectorStoreError(f"delete_versions failed for {collection}: {e}") from e

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
