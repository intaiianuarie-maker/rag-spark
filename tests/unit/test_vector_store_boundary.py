"""Storage-boundary rejects + rebuild safety (T-V1-015 · RP-10 / QF-10).

`retrieval.adapters.qdrant_store` is the single vector-write boundary: both the
ingest pipeline and `scripts/reindex_all_chunks.py` reach Qdrant through its
`upsert`, so this is where invalid embeddings must die — *before* any write —
rather than as a per-caller check in each of them.

Two properties are pinned here:

1. **Rejects before writing.** Zero / NaN / Inf / wrong-dimension vectors raise
   ``VectorStoreInvalidVector`` with a stable ``reason`` and the client sees
   *zero* upsert calls — no partial batch, no poisoned index.
2. **The prior index survives a failed rebuild.** ``ensure_collection`` refuses
   to destroy a populated collection in place, and ``promote_replacement``
   verifies read-only before it performs any destructive call.

Everything runs against an in-stub client: no services, no network, no
``qdrant`` container (tests/README.md — unit tests must be green with compose
down).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from retrieval.adapters.qdrant_store import (
    DENSE_NAME,
    REJECT_DIMENSION,
    REJECT_NON_FINITE,
    REJECT_ZERO,
    SPARSE_NAME,
    QdrantVectorStore,
    VectorStoreError,
    VectorStoreInvalidVector,
)
from retrieval.embedding_identity import EmbeddingIdentity
from retrieval.interfaces import VectorRecord

pytestmark = [pytest.mark.unit]

DIM = 4
# T-R05: this suite is offline and names no live collection. The names are
# scratch-shaped so a reviewer can see at a glance that the live name is not
# here by accident; the stub client means no Qdrant is contacted either way.
LIVE = "knowledge_test_boundary"
REPLACEMENT = "knowledge_test_boundary_replacement"
SERVING = "knowledge_test_boundary__serving"


# --------------------------------------------------------------------------------------
# Stub client — records every mutating call so "no write happened" is observable
# --------------------------------------------------------------------------------------


class StubQdrantClient:
    """Enough of ``AsyncQdrantClient`` to exercise the write boundary offline.

    Collections hold ``VectorRecord``s keyed by id so a test can assert on what
    a rebuild did (or, more often, did *not*) touch.
    """

    def __init__(self) -> None:
        self.dimensions: dict[str, int] = {}
        self.points: dict[str, dict[str, object]] = {}
        self.aliases: dict[str, str] = {}
        self.upsert_calls: list[tuple[str, list[object]]] = []
        self.deleted: list[str] = []
        self.alias_calls: list[list[object]] = []

    # -- helpers (test-side) ------------------------------------------------------------
    def seed(self, name: str, *, points: int = 0, dimension: int = DIM) -> None:
        """Pre-populate a collection with ``points`` placeholder records."""
        self.dimensions[name] = dimension
        self.points[name] = {
            f"seed-{i}": VectorRecord(
                id=f"seed-{i}",
                vector=[0.1] * dimension,
                content=f"seed {i}",
                workspace_id="ws-a",
            )
            for i in range(points)
        }

    def _target(self, name: str) -> str:
        return self.aliases.get(name, name)

    # -- client API used by the store ---------------------------------------------------
    async def collection_exists(self, collection_name: str) -> bool:
        return self._target(collection_name) in self.dimensions

    async def get_collection(self, collection_name: str) -> SimpleNamespace:
        target = self._target(collection_name)
        if target not in self.dimensions:
            raise KeyError(f"Collection {collection_name} doesn't exist!")
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={DENSE_NAME: SimpleNamespace(size=self.dimensions[target])},
                    # Real hybrid collections (verified against a live Qdrant 1.19
                    # instance) declare a named sparse vector; the store's
                    # sparse-declared check (T-I09) reads exactly this shape.
                    sparse_vectors={SPARSE_NAME: SimpleNamespace()},
                )
            ),
            points_count=len(self.points.get(target, {})),
        )

    async def create_collection(
        self, *, collection_name: str, vectors_config: Any, **kwargs: Any
    ) -> None:
        size = vectors_config[DENSE_NAME].size
        self.dimensions[collection_name] = int(size)
        self.points.setdefault(collection_name, {})

    async def delete_collection(self, collection_name: str) -> bool:
        target = self._target(collection_name)
        self.dimensions.pop(target, None)
        self.points.pop(target, None)
        for alias, pointed in list(self.aliases.items()):
            if pointed == target:
                self.aliases.pop(alias, None)
        self.deleted.append(collection_name)
        return True

    async def upsert(self, *, collection_name: str, points: list[Any], **kwargs: Any) -> None:
        target = self._target(collection_name)
        if target not in self.dimensions:
            raise KeyError(f"Collection {collection_name} doesn't exist!")
        self.upsert_calls.append((collection_name, points))
        stored = self.points.setdefault(target, {})
        for point in points:
            stored[point.id] = point

    async def get_aliases(self) -> SimpleNamespace:
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=alias, collection_name=target)
                for alias, target in self.aliases.items()
            ]
        )

    async def update_collection_aliases(
        self, *, change_aliases_operations: list[object], **kwargs: object
    ) -> bool:
        self.alias_calls.append(change_aliases_operations)
        for operation in change_aliases_operations:
            create = getattr(operation, "create_alias", None)
            if create is not None:
                self.aliases[create.alias_name] = create.collection_name
            delete = getattr(operation, "delete_alias", None)
            if delete is not None:
                self.aliases.pop(delete.alias_name, None)
        return True

    async def retrieve(self, **kwargs: object) -> list[object]:
        return []

    async def close(self) -> None:
        return None


def make_store() -> tuple[QdrantVectorStore, StubQdrantClient]:
    client = StubQdrantClient()
    store = QdrantVectorStore(collection=LIVE, client=client)  # type: ignore[arg-type]
    return store, client


def make_live_store() -> tuple[QdrantVectorStore, StubQdrantClient]:
    """A live collection, the way the ingest pipeline leaves it."""
    store, client = make_store()
    client.seed(LIVE, points=3)
    return store, client


#: T-R06b: the boundary verifies embedding identity, so the fixture records
#: carry one. Built with dev2's stamping helpers, never by hand.
IDENTITY = EmbeddingIdentity(
    provider="BoundaryStub", model="stub-model", revision="stub-rev1", dimension=DIM
)


def record(
    *, id: str = "chunk-1", vector: list[float] | None = None, metadata: dict | None = None
) -> VectorRecord:
    return VectorRecord(
        id=id,
        vector=[0.5] * DIM if vector is None else vector,
        content="alpha evidence about permissions",
        workspace_id="ws-a",
        document_id="doc-a",
        chunk_id=id,
        metadata=IDENTITY.as_metadata() if metadata is None else metadata,
    )


# --------------------------------------------------------------------------------------
# 1. Rejects BEFORE any write — the four invalid-vector classes
# --------------------------------------------------------------------------------------


async def test_reject_zero_vector_before_write() -> None:
    """An all-zero (manufactured) embedding never reaches the client."""
    store, client = make_live_store()
    await store.ensure_collection(collection=LIVE, dimension=DIM)

    with pytest.raises(VectorStoreInvalidVector) as excinfo:
        await store.upsert(collection=LIVE, records=[record(vector=[0.0] * DIM)])

    assert excinfo.value.reason == REJECT_ZERO
    assert excinfo.value.record_id == "chunk-1"
    assert client.upsert_calls == [], "a rejected vector must cause ZERO writes"


async def test_reject_nan_vector_before_write() -> None:
    """NaN anywhere in the vector is rejected, not silently indexed."""
    store, client = make_live_store()
    await store.ensure_collection(collection=LIVE, dimension=DIM)
    vector = [0.1, float("nan"), 0.3, 0.4]

    with pytest.raises(VectorStoreInvalidVector) as excinfo:
        await store.upsert(collection=LIVE, records=[record(vector=vector)])

    assert excinfo.value.reason == REJECT_NON_FINITE
    assert client.upsert_calls == [], "a rejected vector must cause ZERO writes"


async def test_reject_infinite_vector_before_write() -> None:
    """+Inf / -Inf are rejected, not silently indexed."""
    store, client = make_live_store()
    await store.ensure_collection(collection=LIVE, dimension=DIM)

    for value in (float("inf"), float("-inf")):
        with pytest.raises(VectorStoreInvalidVector) as excinfo:
            await store.upsert(collection=LIVE, records=[record(vector=[0.1, value, 0.3, 0.4])])
        assert excinfo.value.reason == REJECT_NON_FINITE

    assert client.upsert_calls == [], "a rejected vector must cause ZERO writes"


async def test_reject_wrong_dimension_vector_before_write() -> None:
    """A vector whose length differs from the collection's is rejected."""
    store, client = make_live_store()
    await store.ensure_collection(collection=LIVE, dimension=DIM)

    for wrong in ([0.1, 0.2, 0.3], [0.1, 0.2, 0.3, 0.4, 0.5], []):
        with pytest.raises(VectorStoreInvalidVector) as excinfo:
            await store.upsert(collection=LIVE, records=[record(vector=list(wrong))])
        assert excinfo.value.reason == REJECT_DIMENSION

    assert client.upsert_calls == [], "a rejected vector must cause ZERO writes"


# --------------------------------------------------------------------------------------
# 2. Batch atomicity + honest acceptance (what a stubbed reindex writes)
# --------------------------------------------------------------------------------------


async def test_one_bad_record_blocks_the_whole_batch() -> None:
    """A batch is all-or-nothing: the valid records are not written either."""
    store, client = make_live_store()
    await store.ensure_collection(collection=LIVE, dimension=DIM)
    batch = [
        record(id="ok-1", vector=[0.5] * DIM),
        record(id="bad-1", vector=[0.0] * DIM),
        record(id="ok-2", vector=[0.5] * DIM),
    ]

    with pytest.raises(VectorStoreInvalidVector) as excinfo:
        await store.upsert(collection=LIVE, records=batch)

    assert excinfo.value.record_id == "bad-1"
    assert client.upsert_calls == []
    assert set(client.points[LIVE]) == {"seed-0", "seed-1", "seed-2"}


async def test_valid_batch_is_written_through_the_boundary() -> None:
    """Control: a clean batch still lands, so the guard rejects nothing valid."""
    store, client = make_live_store()
    await store.ensure_collection(collection=LIVE, dimension=DIM)
    batch = [
        record(id="ok-1", vector=[0.5] * DIM),
        record(id="ok-2", vector=[-0.25] * DIM),
    ]

    written = await store.upsert(collection=LIVE, records=batch)

    assert written == 2
    assert len(client.upsert_calls) == 1
    # Point ids pass through the store's deterministic id mapping, so count them.
    assert len(client.points[LIVE]) == 3 + 2, "the 3 pre-existing points plus 2 new ones"


async def test_empty_batch_is_a_no_op_without_a_dimension_lookup() -> None:
    store, client = make_live_store()

    assert await store.upsert(collection=LIVE, records=[]) == 0
    assert client.upsert_calls == []


async def test_write_is_refused_when_the_dimension_cannot_be_verified() -> None:
    """Fail closed: no known collection ⇒ no unverified write."""
    store, client = make_store()  # nothing seeded: dimension is unknown

    with pytest.raises(VectorStoreError):
        await store.upsert(collection=LIVE, records=[record()])

    assert client.upsert_calls == []


# --------------------------------------------------------------------------------------
# 3. Failed / interrupted rebuild keeps the prior index serving
# --------------------------------------------------------------------------------------


async def test_recreate_refuses_to_destroy_a_populated_index() -> None:
    """``recreate=True`` may not take the live index down to start a rebuild."""
    store, client = make_live_store()

    with pytest.raises(VectorStoreError, match="refusing recreate=True"):
        await store.ensure_collection(collection=LIVE, dimension=DIM, recreate=True)

    assert client.deleted == [], "the prior index must not be deleted"
    assert len(client.points[LIVE]) == 3, "the prior index must keep its points"


async def test_recreate_still_works_on_an_empty_or_absent_collection() -> None:
    """Guardrail must not block genuinely empty targets."""
    store, client = make_store()
    client.seed(LIVE, points=0)

    await store.ensure_collection(collection=LIVE, dimension=DIM, recreate=True)

    assert client.dimensions[LIVE] == DIM


async def test_failed_rebuild_promotion_leaves_prior_index_serving() -> None:
    """An incomplete replacement is never promoted: the prior index stands."""
    store, client = make_live_store()
    client.seed(REPLACEMENT, points=2)  # rebuild died after 2 of 3 points

    with pytest.raises(VectorStoreError, match="incomplete"):
        await store.promote_replacement(live=LIVE, replacement=REPLACEMENT, expected_points=3)

    assert client.deleted == [], "no destructive call may happen on verification failure"
    assert client.aliases == {}
    assert len(client.points[LIVE]) == 3, "the prior index still serves"
    assert len(client.points[REPLACEMENT]) == 2


async def test_promotion_of_a_missing_replacement_leaves_prior_index_serving() -> None:
    store, client = make_live_store()

    with pytest.raises(VectorStoreError, match="does not exist"):
        await store.promote_replacement(live=LIVE, replacement=REPLACEMENT, expected_points=1)

    assert client.deleted == []
    assert client.aliases == {}
    assert len(client.points[LIVE]) == 3


async def test_promotion_with_dimension_mismatch_leaves_prior_index_serving() -> None:
    store, client = make_live_store()
    client.seed(REPLACEMENT, points=3, dimension=DIM * 2)

    with pytest.raises(VectorStoreError, match="dimension mismatch"):
        await store.promote_replacement(live=LIVE, replacement=REPLACEMENT, expected_points=3)

    assert client.deleted == []
    assert len(client.points[LIVE]) == 3


async def test_verified_replacement_is_switched_in() -> None:
    """Happy path: the serving ALIAS is repointed atomically, nothing deleted.

    T-R05 replaced the old behaviour, which deleted the serving collection and
    then re-issued the name as an alias — a two-step shape whose failure mode
    is "no index at all". The serving name is now adopted as an alias first
    (additive) and promotion is a single atomic repoint that retains the
    superseded collection as the rollback target.
    """
    store, client = make_live_store()
    client.seed(REPLACEMENT, points=3)

    adopted = await store.ensure_serving_alias(physical=LIVE, alias=SERVING)
    assert adopted.target == LIVE
    assert client.deleted == [], "adoption is additive: no collection is deleted"

    result = await store.promote_replacement(
        live=SERVING, replacement=REPLACEMENT, expected_points=3
    )

    assert client.aliases[SERVING] == REPLACEMENT
    assert len(client.points[REPLACEMENT]) == 3
    # T-R05: the previous physical collection survives for rollback.
    assert result.retained_previous is True
    assert result.previous_target == LIVE
    assert len(client.points[LIVE]) == 3, "the superseded index is retained, not deleted"
    assert client.deleted == [], "promotion deletes nothing"


async def test_promotion_refuses_a_serving_name_that_is_a_physical_collection() -> None:
    """The delete-to-promote shape is refused, not performed."""
    store, client = make_live_store()
    client.seed(REPLACEMENT, points=3)

    with pytest.raises(VectorStoreError, match="physical collection, not an alias"):
        await store.promote_replacement(live=LIVE, replacement=REPLACEMENT, expected_points=3)

    assert client.deleted == [], "no serving data may be deleted to make room"
    assert len(client.points[LIVE]) == 3, "the prior index still serves"
    assert len(client.points[REPLACEMENT]) == 3, "the replacement stays intact"
