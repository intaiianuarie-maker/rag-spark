"""Processing-generation schema, candidate rebuild & atomic serving switch.

T-OR-10. Owns the *generation ledger* -- the durable record of which build of a
collection is ``building`` / ``active`` / ``superseded`` / ``failed`` -- and
the *state machine* that moves a rebuild from candidate into serving without
ever dropping the live index.

This is the in-process glue between the rebuild script (which writes points
into a fresh physical collection, see ``scripts/rebuild_with_manifest.py``) and
the Qdrant serving contract in :mod:`retrieval.adapters.qdrant_store`
(``promote_replacement`` / ``rollback_promotion`` / ``ensure_serving_alias``).
It deliberately does NOT own the rebuild itself: it only registers a
candidate, verifies it, promotes it atomically and -- on failure -- rolls back.

The safety guarantees, all inherited from the store and reinforced here:

* a promotion is **one atomic alias repoint** -- never a delete-then-rename
  (T-R05); the superseded physical collection is **retained** as the rollback
  target;
* **verification is read-only and happens before any destructive call** -- a
  failed or interrupted rebuild leaves the prior generation serving exactly as
  before (T-V1-015);
* a candidate that fails verification is left ``building`` for inspection,
  never promoted incomplete and never deleted by this module (cleanup is an
  explicit operator step);
* every transition is recorded to the ``processing_generations`` table so the
  durable state and the alias never drift.

No Qdrant type crosses this module's boundary: it talks to
:class:`QdrantVectorStore` only through the promotion seam it exposes, and to
the database only through an injected DB gateway (so unit tests run with no
Postgres).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from retrieval.adapters.qdrant_store import (
    PromotionResult,
    QdrantVectorStore,
    VectorStoreError,
)
from retrieval.serving import serving_alias_name

logger = logging.getLogger("knowledge_os.retrieval.generation")

#: Prefix applied to the physical collection name of a freshly built candidate.
#: The candidate is built under this name and only promoted to the serving
#: alias once verified.
CANDIDATE_COLLECTION_PREFIX = "processing_generation__"


class GenerationStatus(StrEnum):
    """The four states a processing_generation row may hold.

    Mirrors the CHECK constraint on the ``status`` column of the
    ``processing_generations`` table (``0011_processing_generations``) so the
    in-process and on-disk vocabularies cannot drift.
    """

    BUILDING = "building"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class GenerationError(RuntimeError):
    """Raised when a generation transition is refused or cannot complete."""


class ProcessingGeneration:
    """A snapshot of a ``processing_generations`` ledger row.

    Instances are treated as immutable snapshots -- the
    :class:`GenerationManager` writes new rows / updates status rather than
    mutating these in place. Tests can compare by value via ``__eq__``.
    """

    __slots__ = (
        "chunker_version",
        "collection_name",
        "created_at",
        "embedding_model",
        "expected_points",
        "generation_id",
        "metadata",
        "points_count",
        "promoted_at",
        "serving_alias",
        "status",
    )

    def __init__(
        self,
        generation_id: str,
        collection_name: str,
        serving_alias: str,
        chunker_version: str,
        embedding_model: str,
        status: GenerationStatus | str = GenerationStatus.BUILDING,
        points_count: int = 0,
        expected_points: int = 0,
        created_at: datetime | None = None,
        promoted_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.generation_id = generation_id
        self.collection_name = collection_name
        self.serving_alias = serving_alias
        self.chunker_version = chunker_version
        self.embedding_model = embedding_model
        self.status = GenerationStatus(status)
        self.points_count = points_count
        self.expected_points = expected_points
        self.created_at = created_at
        self.promoted_at = promoted_at
        self.metadata: dict[str, Any] = dict(metadata) if metadata else {}

    @property
    def physical_collection(self) -> str:
        """The physical Qdrant collection backing this generation.

        For a candidate this is the scratch name (``processing_generation__<id>``);
        for an active/superseded generation this is the live physical collection
        it was promoted from, recorded in ``collection_name`` at creation.
        """
        return self.collection_name

    @property
    def is_active(self) -> bool:
        return self.status is GenerationStatus.ACTIVE

    def copy_with(
        self,
        *,
        status: GenerationStatus | str | None = None,
        points_count: int | None = None,
        promoted_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProcessingGeneration:
        """Return a copy with selective fields overridden."""
        return ProcessingGeneration(
            generation_id=self.generation_id,
            collection_name=self.collection_name,
            serving_alias=self.serving_alias,
            chunker_version=self.chunker_version,
            embedding_model=self.embedding_model,
            status=GenerationStatus(status) if status is not None else self.status,
            points_count=self.points_count if points_count is None else points_count,
            expected_points=self.expected_points,
            created_at=self.created_at,
            promoted_at=promoted_at if promoted_at is not None else self.promoted_at,
            metadata=metadata if metadata is not None else dict(self.metadata),
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"ProcessingGeneration(id={self.generation_id!r}, "
            f"alias={self.serving_alias!r}, status={self.status.value})"
        )

    def __eq__(self, other: object) -> bool:  # pragma: no cover - value equality
        if not isinstance(other, ProcessingGeneration):
            return NotImplemented
        return (
            self.generation_id == other.generation_id
            and self.collection_name == other.collection_name
            and self.serving_alias == other.serving_alias
            and self.chunker_version == other.chunker_version
            and self.embedding_model == other.embedding_model
            and self.status == other.status
            and self.points_count == other.points_count
            and self.expected_points == other.expected_points
        )


class DbGateway(Protocol):
    """Minimal DB surface the manager needs, decoupled from SQLAlchemy.

    The real implementation is SQLAlchemy-backed (see the migration); the unit
    tests provide an in-process fake. Keeping this a Protocol lets the manager
    be tested with NO database.
    """

    def get_generation(self, generation_id: str) -> ProcessingGeneration | None: ...
    def get_active(self, serving_alias: str) -> ProcessingGeneration | None: ...
    def list_generations(self, serving_alias: str) -> list[ProcessingGeneration]: ...
    def insert(self, generation: ProcessingGeneration) -> None: ...
    def update_status(
        self,
        generation_id: str,
        status: GenerationStatus,
        *,
        points_count: int | None = None,
        promoted_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None: ...


class GenerationManager:
    """Transaction script over the generation ledger + Qdrant serving alias.

    Parameters
    ----------
    store:
        The Qdrant vector store. Promotion is delegated to its atomic
        ``promote_replacement`` / ``rollback_promotion``; this manager only
        drives the DB ledger around those calls so the two stay consistent.
    db:
        A :class:`DbGateway` (SQLAlchemy-backed in production). Injected so
        unit tests run without Postgres.
    dimension:
        The dense vector dimension a generation is built against. Used to
        ``ensure_collection`` the candidate scratch collection before points
        are written into it by the rebuild path.
    chunker_version / embedding_model:
        Default identity stamped on every candidate generation. Recorded so a
        verify/promote can refuse an unexpected model change.
    """

    def __init__(
        self,
        *,
        store: QdrantVectorStore,
        db: DbGateway,
        dimension: int = 3072,
        chunker_version: str = "v1",
        embedding_model: str = "google/gemini-embedding-2",
    ) -> None:
        if not store or not db:
            raise GenerationError("store and db are required")
        if not dimension or dimension <= 0:
            raise GenerationError(f"dimension must be positive, got {dimension}")
        self._store = store
        self._db = db
        self._dimension = dimension
        self._chunker_version = chunker_version
        self._embedding_model = embedding_model

    # -------------------------------------------------------------- #
    # candidate creation
    # -------------------------------------------------------------- #
    async def create_candidate_generation(
        self,
        *,
        collection_name: str,
        chunker_version: str | None = None,
        embedding_model: str | None = None,
        expected_points: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProcessingGeneration:
        """Register a candidate generation against a *physical* collection.

        This does NOT write points into the collection -- the rebuild script
        (``scripts/rebuild_with_manifest.py``) does that against the physical
        name returned here
        (:attr:`ProcessingGeneration.physical_collection`). It only:

        * rejects an empty ``collection_name``;
        * allocates a fresh ``generation_id`` and physical collection name;
        * ensures the scratch physical collection exists with the right
          dimension (``ensure_collection``, additive -- a populated collection
          is never destroyed here);
        * records a ``building`` row in the ledger, stamped with the active
          generation's embedding model as the compatibility baseline.

        The candidate starts ``building`` -- it is not serving until
        :meth:`promote_candidate` succeeds.
        """
        if not collection_name or not collection_name.strip():
            raise GenerationError("collection_name is required")
        serving_alias = serving_alias_name(collection_name)
        chunker = chunker_version or self._chunker_version
        model = embedding_model or self._embedding_model

        # The candidate is built under a *scratch* physical collection name
        # (``processing_generation__<id>``) separate from the serving alias, so
        # the rebuild never touches the live collection. The scratch name is what
        # this generation actually backs, so it is recorded as ``collection_name``
        # (the physical collection for this generation); the stable
        # ``serving_alias`` is derived from the caller's base ``collection_name``.
        generation_id = uuid.uuid4().hex
        physical_collection = CANDIDATE_COLLECTION_PREFIX + generation_id

        baseline = self._db.get_active(serving_alias)
        gen_meta: dict[str, Any] = dict(metadata) if metadata else {}
        # Always record the baseline identity (None when there is no prior
        # active generation, i.e. this is the first build of the alias); this
        # keeps the verify/promote compatibility contract uniform.
        gen_meta["baseline_generation_id"] = (
            baseline.generation_id if baseline is not None else None
        )
        gen_meta["baseline_embedding_model"] = (
            baseline.embedding_model if baseline is not None else None
        )

        generation = ProcessingGeneration(
            generation_id=generation_id,
            collection_name=physical_collection,
            serving_alias=serving_alias,
            chunker_version=chunker,
            embedding_model=model,
            status=GenerationStatus.BUILDING,
            points_count=0,
            expected_points=expected_points,
            created_at=datetime.now(UTC),
            metadata=gen_meta,
        )

        # Provision the scratch physical collection (additive: never destroys a
        # populated collection; the rebuild writes points into it afterwards).
        await self._ensure_scratch_collection(generation.physical_collection, self._dimension)

        self._db.insert(generation)
        logger.info(
            "generation_candidate_registered",
            extra={
                "event": "generation_candidate_registered",
                "generation_id": generation_id,
                "serving_alias": serving_alias,
                "physical_collection": generation.physical_collection,
                "status": generation.status.value,
            },
        )
        return generation

    # -------------------------------------------------------------- #
    # verification
    # -------------------------------------------------------------- #
    async def verify_candidate_rebuild(self, generation_id: str) -> ProcessingGeneration:
        """Validate a candidate's point count before promotion.

        Two checks, both **read-only** (no destructive call in the failure
        path):

        1. point count -- the physical collection must hold at least
           ``expected_points`` (the manifest count the rebuild promised). A
           partial rebuild is refused and left ``building`` for inspection.
        2. embedding identity -- the candidate's embedding model must match the
           active generation's (when one exists); a model change must go
           through a full rebuild of the alias, not a same-model candidate.

        Returns the (possibly updated) generation. Raises
        :class:`GenerationError` on mismatch; the candidate is NOT promoted and
        the live alias is NOT touched.
        """
        generation = self._db.get_generation(generation_id)
        if generation is None:
            raise GenerationError(f"generation {generation_id!r} not found")
        if generation.status is not GenerationStatus.BUILDING:
            raise GenerationError(
                f"generation {generation_id!r} is {generation.status.value}, "
                "not building -- only a building candidate may be verified"
            )

        # --- check 1: point count (the manifest count recorded at creation) --
        try:
            actual_points = await self._store._point_count(generation.physical_collection)
        except VectorStoreError as exc:
            raise GenerationError(
                f"could not read point count for {generation.physical_collection!r} "
                f"({exc}); nothing promoted, candidate left building for inspection"
            ) from exc

        if actual_points < generation.expected_points:
            # Record the observed count but keep status BUILDING so an operator
            # can inspect rather than be forced into a failed state for a
            # transiently slow build.
            self._db.update_status(
                generation.generation_id,
                GenerationStatus.BUILDING,
                points_count=actual_points,
            )
            raise GenerationError(
                f"candidate {generation_id!r} is incomplete: {actual_points} of "
                f"{generation.expected_points} expected points -- nothing promoted. "
                "Either let the rebuild finish and re-verify, or delete the "
                f"half-built candidate collection {generation.physical_collection!r} "
                "and re-run the rebuild."
            )

        # --- check 2: embedding identity compatibility ----------------------
        active = self._db.get_active(generation.serving_alias)
        if active is not None and not _identity_compatible(generation, active):
            raise GenerationError(
                f"candidate {generation_id!r} ({generation.embedding_model!r}) is "
                f"incompatible with the active generation {active.generation_id!r} "
                f"({active.embedding_model!r}) -- nothing promoted. A model change "
                "must go through a full rebuild of the serving alias, not a "
                "same-model candidate."
            )

        # Persist the observed point count; status stays BUILDING until promote.
        self._db.update_status(
            generation.generation_id,
            GenerationStatus.BUILDING,
            points_count=actual_points,
        )
        verified = self._db.get_generation(generation_id)
        assert verified is not None  # just wrote it
        logger.info(
            "generation_candidate_verified",
            extra={
                "event": "generation_candidate_verified",
                "generation_id": generation_id,
                "points": actual_points,
                "expected": generation.expected_points,
            },
        )
        return verified

    # -------------------------------------------------------------- #
    # promotion
    # -------------------------------------------------------------- #
    async def promote_candidate(self, generation_id: str) -> PromotionResult:
        """Atomically repoint the serving alias onto a verified candidate.

        Delegates the switch to ``QdrantVectorStore.promote_replacement``,
        which is **one atomic alias repoint** (a single
        ``update_collection_aliases`` request carrying delete+create) and
        verifies the replacement (exists, dimension, point count) BEFORE the
        repoint while performing NO destructive call on failure. The serving
        name must already BE an alias (``ensure_serving_alias`` was applied
        during the live collection's initial adoption), so promotion never
        deletes the serving name and never deletes the superseded physical
        collection -- both are retained.

        After the alias is repointed, the ledger is updated: the candidate
        becomes ``active`` and the prior ``active`` generation (if any)
        becomes ``superseded``. If verification inside the store fails,
        nothing in the ledger is touched because the alias was not touched.
        """
        generation = self._db.get_generation(generation_id)
        if generation is None:
            raise GenerationError(f"generation {generation_id!r} not found")
        if generation.status is not GenerationStatus.BUILDING:
            raise GenerationError(
                f"generation {generation_id!r} is {generation.status.value}; "
                "only a building candidate may be promoted"
            )

        live = generation.serving_alias
        replacement = generation.physical_collection
        prior_active = self._db.get_active(live)

        # The store verifies the replacement BEFORE the single atomic repoint
        # and performs no destructive call on failure -- so a failed promotion
        # leaves the prior generation serving exactly as before and the ledger
        # is left untouched (no state change to record).
        result: PromotionResult = await self._store.promote_replacement(
            live=live,
            replacement=replacement,
            expected_points=generation.expected_points,
        )

        # Alias repointed successfully: commit the ledger transition.
        now = datetime.now(UTC)
        if prior_active is not None:
            self._db.update_status(prior_active.generation_id, GenerationStatus.SUPERSEDED)
        self._db.update_status(
            generation.generation_id,
            GenerationStatus.ACTIVE,
            points_count=generation.points_count,
            promoted_at=now,
            metadata={
                **dict(generation.metadata),
                "promoted_from": prior_active.generation_id if prior_active else None,
                "previous_target": result.previous_target,
            },
        )
        logger.info(
            "generation_promoted",
            extra={
                "event": "generation_promoted",
                "generation_id": generation_id,
                "serving_alias": live,
                "previous_target": result.previous_target,
                "new_target": replacement,
                "retained_previous": result.retained_previous,
            },
        )
        return result

    # -------------------------------------------------------------- #
    # rollback
    # -------------------------------------------------------------- #
    async def rollback_generation(
        self, serving_alias: str, *, to: str | None = None
    ) -> PromotionResult:
        """Roll the serving alias back to a retained prior target.

        Delegates to ``QdrantVectorStore.rollback_promotion``, which repoints
        the alias back to the prior physical collection (verified readable
        first) and is itself an atomic single request. After a successful
        rollback the ledger is reconciled: the generation that was serving is
        marked ``failed`` (it is no longer current), and the rolled-back-to
        generation is marked ``active``.

        ``to`` may be a physical collection name OR a generation_id; when it
        is a known generation_id it is resolved to that generation's physical
        collection, so callers can roll back to "generation X" without knowing
        the physical name. When ``to`` is omitted the store uses the last-known
        previous target it recorded at promotion time.
        """
        live = serving_alias

        # Resolve a generation_id to its physical collection if the caller named
        # a generation rather than a physical name.
        target = to
        if target is not None:
            gen = self._db.get_generation(target)
            if gen is not None:
                target = gen.physical_collection

        # The store verifies the rollback target is readable BEFORE the repoint
        # and performs no destructive call on failure.
        result = await self._store.rollback_promotion(live, to=target)

        now = datetime.now(UTC)
        new_current = result.new_target
        # Reconcile the ledger against the new alias target.
        for gen in self._db.list_generations(live):
            if gen.physical_collection == new_current:
                self._db.update_status(gen.generation_id, GenerationStatus.ACTIVE, promoted_at=now)
            elif gen.status is GenerationStatus.ACTIVE:
                # Was serving, is no longer the current target.
                self._db.update_status(gen.generation_id, GenerationStatus.FAILED)
        logger.info(
            "generation_rolled_back",
            extra={
                "event": "generation_rolled_back",
                "serving_alias": live,
                "previous_target": result.previous_target,
                "new_target": result.new_target,
                "recovery_command": result.recovery_command,
            },
        )
        return result

    # -------------------------------------------------------------- #
    # read helpers
    # -------------------------------------------------------------- #
    def current_generation(self, serving_alias: str) -> ProcessingGeneration | None:
        """The active generation for ``serving_alias``, or ``None``."""
        return self._db.get_active(serving_alias)

    # -------------------------------------------------------------- #
    # internal
    # -------------------------------------------------------------- #
    async def _ensure_scratch_collection(self, physical: str, dimension: int) -> None:
        """Provision the candidate's scratch physical collection (additive).

        ``ensure_collection`` with ``recreate=False`` so an existing empty
        collection is reused and a populated one is left untouched -- this
        never destroys a previous build. A ``VectorStoreError`` on a populated
        target is translated into a :class:`GenerationError`.
        """
        try:
            await self._store.ensure_collection(collection=physical, dimension=dimension)
        except VectorStoreError as exc:
            raise GenerationError(
                f"could not provision scratch collection {physical!r}: {exc}; a "
                "previous build may still be writing to it -- inspect and clean up first"
            ) from exc


def _identity_compatible(candidate: ProcessingGeneration, active: ProcessingGeneration) -> bool:
    """Two generations are compatible only if their embedding model matches.

    A full compatibility check would also compare provider/revision/dimension
    (see ``retrieval.embedding_identity``); for the ledger we require at least
    the model match, since a rebuild targeting a different provider is
    intentionally a full-rebuild, not a same-model candidate.
    """
    return candidate.embedding_model == active.embedding_model


__all__ = [
    "CANDIDATE_COLLECTION_PREFIX",
    "DbGateway",
    "GenerationError",
    "GenerationManager",
    "GenerationStatus",
    "ProcessingGeneration",
]
