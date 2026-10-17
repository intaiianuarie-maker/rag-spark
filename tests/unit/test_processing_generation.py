"""T-OR-10 — unit tests for the processing-generation ledger + atomic switch.

Runs entirely on in-process fakes for both the Qdrant vector store and the
DB gateway, so it needs no external services (no Postgres, no Qdrant) --
consistent with the unit-test contract in tests/conftest.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from retrieval.adapters.qdrant_store import PromotionResult, VectorStoreError
from retrieval.generation import (
    CANDIDATE_COLLECTION_PREFIX,
    GenerationError,
    GenerationManager,
    GenerationStatus,
    ProcessingGeneration,
)

pytestmark = [pytest.mark.unit]

DEFAULT_DIM = 3072
MODEL = "google/gemini-embedding-2"


class FakeStore:
    """In-process double for QdrantVectorStore's promotion seam.

    It records every destructive call so tests can assert the core safety
    property of T-R05/T-V1-015: a promotion never deletes the serving
    collection or the previous target.
    """

    def __init__(self, *, point_counts: dict[str, int] | None = None) -> None:
        # physical collection -> point count the fake reports.
        self._points: dict[str, int] = dict(point_counts) if point_counts else {}
        # serving alias -> physical collection it currently resolves to.
        self._alias_target: dict[str, str] = {}
        # last-known previous target per alias (for rollback_promotion default).
        self._prev_target: dict[str, str] = {}
        # destructive call log (collection names passed to a (hypothetical)
        # delete; the real store never deletes on promote, and we assert that).
        self.deletes: list[str] = []
        self.recreates: list[str] = []

    async def ensure_collection(
        self, *, collection: str, dimension: int, distance: str = "cosine"
    ) -> None:
        # Additive: create if absent (we track presence implicitly).
        if collection not in self._points:
            self._points[collection] = 0

    async def _point_count(self, coll: str) -> int:
        if coll not in self._points:
            raise VectorStoreError(f"collection {coll!r} does not exist")
        return self._points[coll]

    def _set_points(self, coll: str, count: int) -> None:
        """Test helper: simulate the rebuild writing points into a collection."""
        self._points[coll] = count

    def _adopt_alias(self, alias: str, physical: str) -> None:
        """Test helper: simulate the initial serving-alias adoption (additive).

        Also registers the physical collection in ``_points`` (as a non-empty
        collection) so the live/active collection is a real retained collection,
        matching production where an active index always carries points.
        """
        self._alias_target[alias] = physical
        self._prev_target.setdefault(alias, physical)
        if physical not in self._points:
            self._points[physical] = 1

    async def serving_alias_state(self, alias: str) -> Any:
        from retrieval.serving import ServingAliasState

        if alias in self._alias_target:
            target = self._alias_target[alias]
            return ServingAliasState(alias=alias, target=target, serving_name_is_physical=False)
        if alias in self._points:
            return ServingAliasState(alias=alias, target=alias, serving_name_is_physical=True)
        return ServingAliasState(alias=alias, target=None, serving_name_is_physical=False)

    async def promote_replacement(
        self, *, live: str, replacement: str, expected_points: int = 0
    ) -> PromotionResult:
        # Mirror the real store's verification-then-atomic-repoint semantics.
        if not await self._client_exists(replacement):
            raise VectorStoreError(
                f"replacement {replacement!r} does not exist -- nothing promoted"
            )
        actual = await self._point_count(replacement)
        if actual < expected_points:
            raise VectorStoreError(
                f"replacement {replacement!r} is incomplete: {actual} of "
                f"{expected_points} expected -- nothing promoted"
            )
        previous = self._alias_target.get(live)
        if previous == replacement:
            raise VectorStoreError(f"{live!r} already serves {replacement!r}")
        # The serving name must already be an adopted alias (or a known physical
        # collection acting as its own serving name); otherwise there is nothing
        # to atomically repoint and promotion must be refused -- this is the T-R05
        # guarantee that promotion never deletes the serving name to free it.
        if live not in self._alias_target and live not in self._points:
            raise VectorStoreError(
                f"serving name {live!r} is not an adopted alias -- "
                "use ensure_serving_alias first; nothing promoted"
            )
        # Atomic repoint: delete-alias + create-alias in one logical step.
        self._alias_target[live] = replacement
        self._prev_target[live] = previous
        return PromotionResult(
            serving=live,
            previous_target=previous or "",
            new_target=replacement,
            retained_previous=True,
            recovery_command="",
            executed=True,
        )

    async def rollback_promotion(self, live: str, *, to: str | None = None) -> PromotionResult:
        target = to or self._prev_target.get(live)
        if not target:
            raise VectorStoreError(f"no previous target recorded for {live!r}")
        before = self._alias_target.get(live)
        self._alias_target[live] = target
        self._prev_target[live] = before
        return PromotionResult(
            serving=live,
            previous_target=before or "",
            new_target=target,
            retained_previous=True,
            recovery_command="",
            executed=True,
        )

    async def _client_exists(self, coll: str) -> bool:
        return coll in self._points


class FakeDb:
    """In-process double for the _DbGateway."""

    def __init__(self) -> None:
        self._rows: dict[str, ProcessingGeneration] = {}
        self.inserts: list[ProcessingGeneration] = []
        self.status_changes: list[tuple[str, GenerationStatus]] = []

    def get_generation(self, generation_id: str) -> ProcessingGeneration | None:
        return self._rows.get(generation_id)

    def get_active(self, serving_alias: str) -> ProcessingGeneration | None:
        for gen in self._rows.values():
            if gen.serving_alias == serving_alias and gen.status is GenerationStatus.ACTIVE:
                return gen
        return None

    def list_generations(self, serving_alias: str) -> list[ProcessingGeneration]:
        return [g for g in self._rows.values() if g.serving_alias == serving_alias]

    def insert(self, generation: ProcessingGeneration) -> None:
        self._rows[generation.generation_id] = generation
        self.inserts.append(generation)

    def update_status(
        self,
        generation_id: str,
        status: GenerationStatus,
        *,
        points_count: int | None = None,
        promoted_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        gen = self._rows[generation_id]
        self._rows[generation_id] = gen.copy_with(
            status=status,
            points_count=points_count,
            promoted_at=promoted_at,
            metadata={**dict(gen.metadata), **(metadata or {})},
        )
        self.status_changes.append((generation_id, status))

    def _rows_for(self, alias: str) -> list[ProcessingGeneration]:
        return self.list_generations(alias)


@pytest.fixture
def store_and_db():
    """A fresh (store, db) pair wired into a GenerationManager."""
    store = FakeStore()
    db = FakeDb()
    manager = GenerationManager(store=store, db=db, dimension=DEFAULT_DIM)
    return manager, store, db


# ---------------------------------------------------------------------------
# candidate creation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_candidate_registers_building_generation(store_and_db):
    manager, store, db = store_and_db
    gen = await manager.create_candidate_generation(
        collection_name="knowledge",
        chunker_version="v2",
        embedding_model=MODEL,
        expected_points=1000,
    )
    assert gen.status is GenerationStatus.BUILDING
    assert gen.physical_collection == CANDIDATE_COLLECTION_PREFIX + gen.generation_id
    assert gen.collection_name == gen.physical_collection
    assert gen.serving_alias == "knowledge__serving"
    assert gen.expected_points == 1000
    assert gen.points_count == 0
    assert gen.metadata["baseline_generation_id"] is None  # no prior active

    # persisted to the ledger
    persisted = db.get_generation(gen.generation_id)
    assert persisted is not None
    assert persisted.status is GenerationStatus.BUILDING
    # scratch collection was provisioned additively (no delete, not a recreate)
    assert store.deletes == []
    assert store.recreates == []


@pytest.mark.asyncio
async def test_create_candidate_requires_collection_name(store_and_db):
    manager, _store, _db = store_and_db
    with pytest.raises(GenerationError, match="collection_name is required"):
        await manager.create_candidate_generation(collection_name="  ")


@pytest.mark.asyncio
async def test_create_candidate_records_baseline_from_active(store_and_db):
    manager, store, db = store_and_db
    # pre-seed an active generation as the prior baseline
    prior = ProcessingGeneration(
        generation_id="prior-gen",
        collection_name="knowledge__v1",
        serving_alias="knowledge__serving",
        chunker_version="v1",
        embedding_model=MODEL,
        status=GenerationStatus.ACTIVE,
        expected_points=500,
    )
    db.insert(prior)
    store._adopt_alias("knowledge__serving", "knowledge__v1")

    gen = await manager.create_candidate_generation(
        collection_name="knowledge", expected_points=500
    )
    assert gen.metadata["baseline_generation_id"] == "prior-gen"
    assert gen.metadata["baseline_embedding_model"] == MODEL


# ---------------------------------------------------------------------------
# validation refusal on point mismatch
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_refuses_incomplete_candidate(store_and_db):
    manager, store, db = store_and_db
    gen = await manager.create_candidate_generation(
        collection_name="knowledge", expected_points=1000
    )
    # rebuild wrote fewer points than promised
    store._set_points(gen.physical_collection, 950)

    with pytest.raises(GenerationError, match="is incomplete"):
        await manager.verify_candidate_rebuild(gen.generation_id)

    # candidate stays BUILDING, not promoted
    persisted = db.get_generation(gen.generation_id)
    assert persisted.status is GenerationStatus.BUILDING
    assert persisted.points_count == 950
    # nothing destructive happened
    assert store.deletes == []


@pytest.mark.asyncio
async def test_verify_refuses_incompatible_embedding_model(store_and_db):
    manager, store, db = store_and_db
    prior = ProcessingGeneration(
        generation_id="prior-gen",
        collection_name="knowledge__v1",
        serving_alias="knowledge__serving",
        chunker_version="v1",
        embedding_model="google/gemini-embedding-2",
        status=GenerationStatus.ACTIVE,
        expected_points=500,
    )
    db.insert(prior)

    gen = await manager.create_candidate_generation(
        collection_name="knowledge",
        embedding_model="openai/text-embedding-3-small",
        expected_points=500,
    )
    store._set_points(gen.physical_collection, 500)

    with pytest.raises(GenerationError, match="incompatible"):
        await manager.verify_candidate_rebuild(gen.generation_id)
    # alias untouched
    assert store._alias_target == {}


@pytest.mark.asyncio
async def test_verify_passes_when_points_and_model_match(store_and_db):
    manager, store, db = store_and_db
    prior = ProcessingGeneration(
        generation_id="prior-gen",
        collection_name="knowledge__v1",
        serving_alias="knowledge__serving",
        chunker_version="v1",
        embedding_model=MODEL,
        status=GenerationStatus.ACTIVE,
        expected_points=500,
    )
    db.insert(prior)

    gen = await manager.create_candidate_generation(
        collection_name="knowledge", expected_points=500
    )
    store._set_points(gen.physical_collection, 500)

    verified = await manager.verify_candidate_rebuild(gen.generation_id)
    assert verified.points_count == 500
    # still BUILDING until explicitly promoted
    assert verified.status is GenerationStatus.BUILDING


# ---------------------------------------------------------------------------
# atomic promotion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_promote_makes_candidate_active_and_prior_superseded(store_and_db):
    manager, store, db = store_and_db
    # adopt the serving alias against a prior active physical collection
    store._adopt_alias("knowledge__serving", "knowledge__v1")
    prior = ProcessingGeneration(
        generation_id="prior-gen",
        collection_name="knowledge__v1",
        serving_alias="knowledge__serving",
        chunker_version="v1",
        embedding_model=MODEL,
        status=GenerationStatus.ACTIVE,
        expected_points=500,
    )
    db.insert(prior)

    gen = await manager.create_candidate_generation(
        collection_name="knowledge", expected_points=500
    )
    store._set_points(gen.physical_collection, 500)

    result = await manager.promote_candidate(gen.generation_id)

    # alias repointed atomically
    assert result.executed
    assert result.new_target == gen.physical_collection
    assert result.previous_target == "knowledge__v1"
    assert result.retained_previous is True
    # ledger reconciled
    assert db.get_active("knowledge__serving").generation_id == gen.generation_id
    assert db.get_generation("prior-gen").status is GenerationStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_promote_refuses_without_alias_adopted(store_and_db):
    """Promotion must never delete the serving name to free it (T-R05)."""
    manager, store, db = store_and_db
    gen = await manager.create_candidate_generation(
        collection_name="knowledge", expected_points=500
    )
    store._set_points(gen.physical_collection, 500)
    # alias NOT adopted -> the real store refuses; here the fake also has no
    # alias target, so the candidate physical collection is the only thing
    # and promote_replacement finds no alias to repoint.
    with pytest.raises((GenerationError, VectorStoreError)):
        await manager.promote_candidate(gen.generation_id)
    # candidate remains BUILDING, never activated
    assert db.get_generation(gen.generation_id).status is GenerationStatus.BUILDING


@pytest.mark.asyncio
async def test_promote_failure_leaves_prior_serving_and_ledger_untouched(store_and_db):
    """A failed verification inside promote must not mutate the ledger."""
    manager, store, db = store_and_db
    store._adopt_alias("knowledge__serving", "knowledge__v1")
    prior = ProcessingGeneration(
        generation_id="prior-gen",
        collection_name="knowledge__v1",
        serving_alias="knowledge__serving",
        chunker_version="v1",
        embedding_model=MODEL,
        status=GenerationStatus.ACTIVE,
        expected_points=500,
    )
    db.insert(prior)

    gen = await manager.create_candidate_generation(
        collection_name="knowledge", expected_points=500
    )
    # build it incomplete so the store's verification refuses
    store._set_points(gen.physical_collection, 10)

    with pytest.raises((GenerationError, VectorStoreError)):
        await manager.promote_candidate(gen.generation_id)

    # prior still active, candidate still building
    assert db.get_active("knowledge__serving").generation_id == "prior-gen"
    assert db.get_generation(gen.generation_id).status is GenerationStatus.BUILDING
    # alias never moved
    assert store._alias_target["knowledge__serving"] == "knowledge__v1"


# ---------------------------------------------------------------------------
# zero destructive drops during promotion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_promote_never_drops_serving_or_previous(store_and_db):
    """Core T-R05/T-V1-015 invariant: promotion deletes nothing."""
    manager, store, db = store_and_db
    store._adopt_alias("knowledge__serving", "knowledge__v1")
    prior = ProcessingGeneration(
        generation_id="prior-gen",
        collection_name="knowledge__v1",
        serving_alias="knowledge__serving",
        chunker_version="v1",
        embedding_model=MODEL,
        status=GenerationStatus.ACTIVE,
        expected_points=500,
    )
    db.insert(prior)

    gen = await manager.create_candidate_generation(
        collection_name="knowledge", expected_points=500
    )
    store._set_points(gen.physical_collection, 500)

    await manager.promote_candidate(gen.generation_id)

    # No destructive operations on either the serving name or the superseded
    # physical collection -- the previous target is RETAINED as rollback.
    assert store.deletes == []
    assert store.recreates == []
    # the superseded physical collection is still tracked by the fake store
    assert "knowledge__v1" in store._points


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rollback_repoints_alias_and_reconciles_ledger(store_and_db):
    manager, store, db = store_and_db
    store._adopt_alias("knowledge__serving", "knowledge__v1")
    prior = ProcessingGeneration(
        generation_id="prior-gen",
        collection_name="knowledge__v1",
        serving_alias="knowledge__serving",
        chunker_version="v1",
        embedding_model=MODEL,
        status=GenerationStatus.ACTIVE,
        expected_points=500,
    )
    db.insert(prior)

    gen = await manager.create_candidate_generation(
        collection_name="knowledge", expected_points=500
    )
    store._set_points(gen.physical_collection, 500)
    await manager.promote_candidate(gen.generation_id)

    # now roll back -- to the previous target explicitly by generation_id
    result = await manager.rollback_generation("knowledge__serving", to="prior-gen")
    assert result.executed
    assert result.new_target == "knowledge__v1"
    # alias repointed back
    assert store._alias_target["knowledge__serving"] == "knowledge__v1"
    # the rolled-back-to generation is active again
    assert db.get_active("knowledge__serving").generation_id == "prior-gen"
    # the failed-over generation is marked failed
    assert db.get_generation(gen.generation_id).status is GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_rollback_to_by_physical_name(store_and_db):
    manager, store, db = store_and_db
    store._adopt_alias("knowledge__serving", "knowledge__v1")
    prior = ProcessingGeneration(
        generation_id="prior-gen",
        collection_name="knowledge__v1",
        serving_alias="knowledge__serving",
        chunker_version="v1",
        embedding_model=MODEL,
        status=GenerationStatus.ACTIVE,
        expected_points=500,
    )
    db.insert(prior)

    gen = await manager.create_candidate_generation(
        collection_name="knowledge", expected_points=500
    )
    store._set_points(gen.physical_collection, 500)
    await manager.promote_candidate(gen.generation_id)

    # roll back by physical collection name (not a generation_id)
    result = await manager.rollback_generation("knowledge__serving", to="knowledge__v1")
    assert result.new_target == "knowledge__v1"


@pytest.mark.asyncio
async def test_rollback_failure_does_not_reconcile_ledger(store_and_db):
    """If the store refuses the rollback, the ledger is not mutated."""
    manager, store, db = store_and_db
    # no alias adopted, so rollback_promotion has no previous target
    gen = await manager.create_candidate_generation(
        collection_name="knowledge", expected_points=500
    )
    store._set_points(gen.physical_collection, 500)
    with pytest.raises(VectorStoreError):
        await manager.rollback_generation("knowledge__serving")
    # ledger untouched: candidate still building
    assert db.get_generation(gen.generation_id).status is GenerationStatus.BUILDING


# ---------------------------------------------------------------------------
# current_generation read helper
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_current_generation_none_when_no_active(store_and_db):
    manager, _store, _db = store_and_db
    assert manager.current_generation("knowledge__serving") is None
