"""Versioned draft / revision store for workflow definitions (T-V4-017 / W4).

Definitions need versioned storage before the approval gates (T-V4-018+) can
mean anything: an approval is a decision about *one specific revision*, so the
store must be able to say exactly which revision was approved and hand it back
byte-for-byte later.

What this store guarantees
--------------------------
* **Append-only.** There is no update, delete or overwrite path — not
  "disabled", absent. Revision 1 of a workflow is still revision 1 after
  twenty more revisions, with unchanged content.
* **Monotonic revisions.** Per workflow, revision numbers strictly increase
  and are never reused, including when new content matches an older
  revision's content (a revert is a *new* revision, not a rewrite of history).
* **Content-hash identity.** Every revision's ``content_hash`` comes from
  ``WorkflowRevision.create`` (T-V4-008) — the same canonical, process
  independent SHA-256 the compiler and executor verify. There is exactly one
  hash function in play, so a revision read out of this store is accepted by
  the compiler without re-derivation.
* **Bounded + redacted projections.** Canonical payload size is capped and
  never truncated; a definition whose node config carries a secret is refused
  at the boundary; history listings show a redacted bounded summary only.
* **Immutability.** Callers get deep copies, so neither the caller's caller
  nor a later reader can mutate a stored revision.

The ABC seam is the contract; :class:`InMemoryDraftStore` is the in-memory
implementation for this ticket. A durable backend is a follow-up (the same
shape ``SqliteCheckpointStore`` followed ``InMemoryCheckpointStore`` in
T-V4-016) and is deliberately **not** built here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from .interfaces import WorkflowDefinition, WorkflowRevision
from .store_discipline import (
    MAX_PAYLOAD_BYTES,
    assert_no_secret_config,
    build_summary,
    canonical_bytes,
    enforce_bounds,
)


class DraftRevisionError(RuntimeError):
    """Base class for draft-store refusals."""


class DraftNotFoundError(DraftRevisionError):
    """No such workflow revision in the store."""


class DraftConflictError(DraftRevisionError):
    """The requested operation contradicts existing history."""


class DraftIdentityError(DraftRevisionError):
    """The definition's identity does not match the requested workflow."""


class DraftWorkspaceError(DraftRevisionError):
    """A revision would cross a workspace boundary."""


def verify_revision(revision: WorkflowRevision) -> bool:
    """Recompute the canonical hash of *revision*'s spec and compare.

    ``WorkflowRevision`` is frozen (no top-level reassignment) but its nested
    spec fields can still be mutated in place, so this is the check that a
    stored record is really the record that was hashed.
    """
    return WorkflowRevision.create(revision.spec).content_hash == revision.content_hash


class DraftRevision(BaseModel):
    """One stored revision plus the store's redacted projection of it.

    Wraps the system's own :class:`WorkflowRevision` rather than duplicating
    its fields, so a stored revision can be handed straight to the compiler,
    validator or executor — which is what an approval gate will do.
    """

    model_config = ConfigDict(frozen=True)

    revision: WorkflowRevision
    #: Redacted, bounded one-line projection shown in history listings.
    summary: str = ""
    #: Revision this one supersedes (``None`` for the first).
    parent_revision: int | None = None
    #: Content hash of the parent revision (``None`` for the first).
    parent_hash: str | None = None

    @property
    def workflow_id(self) -> str:
        return self.revision.workflow_id

    @property
    def number(self) -> int:
        return self.revision.revision

    @property
    def content_hash(self) -> str:
        return self.revision.content_hash

    @property
    def workspace_id(self) -> str:
        return self.revision.spec.workspace_id

    @property
    def spec(self) -> WorkflowDefinition:
        return self.revision.spec


class DraftStore(ABC):
    """Versioned, append-only store for workflow definitions.

    The surface is deliberately minimal — create, read, list — with no
    mutating write of any kind.
    """

    @abstractmethod
    async def save_draft(self, definition: WorkflowDefinition) -> DraftRevision:
        """Store *definition* as revision 1 of a new workflow."""
        raise NotImplementedError

    @abstractmethod
    async def revise(
        self, workflow_id: str, definition: WorkflowDefinition
    ) -> DraftRevision:
        """Append a new revision of *workflow_id* from *definition*.

        Identical content to the current head returns that head unchanged
        (idempotent — no history growth, no rewrite).
        """
        raise NotImplementedError

    @abstractmethod
    async def get(self, workflow_id: str, revision: int) -> DraftRevision:
        """Read back revision *revision* of *workflow_id* verbatim."""
        raise NotImplementedError

    @abstractmethod
    async def latest(self, workflow_id: str) -> DraftRevision | None:
        """Current head revision, or ``None`` if the workflow is unknown."""
        raise NotImplementedError

    @abstractmethod
    async def history(self, workflow_id: str) -> list[DraftRevision]:
        """Every revision of *workflow_id*, ascending, immutable."""
        raise NotImplementedError

    @abstractmethod
    async def count(self, workflow_id: str) -> int:
        """Number of revisions stored for *workflow_id*."""
        raise NotImplementedError

    @abstractmethod
    async def workflow_ids(self) -> list[str]:
        """Every workflow id known to the store (ascending)."""
        raise NotImplementedError


#: Method names that would rewrite or drop history. Their absence from the
#: store surface is what "append-only" means operationally.
_FORBIDDEN_WRITE_METHODS = (
    "update",
    "delete",
    "remove",
    "overwrite",
    "replace",
    "clear",
    "truncate",
    "rewrite",
    "purge",
)


def assert_append_only(store: DraftStore) -> None:
    """Assert *store* exposes no history-rewriting or deleting path."""
    exposed = {
        name
        for name in dir(store)
        if not name.startswith("_") and callable(getattr(store, name, None))
    }
    offenders = sorted(exposed.intersection(_FORBIDDEN_WRITE_METHODS))
    if offenders:
        raise DraftConflictError(
            f"draft store exposes history-mutating methods: {offenders}"
        )


class InMemoryDraftStore(DraftStore):
    """Append-only in-memory draft store (durable backend is a follow-up).

    Not concurrency-safe: this is the in-memory implementation behind the
    ABC seam for the W4 lane, and a durable backend will be the concurrent
    one. Callers must not interleave writes to one workflow id.
    """

    def __init__(self) -> None:
        self._history: dict[str, list[DraftRevision]] = {}

    # -- internal ------------------------------------------------------------------ #

    async def _append(
        self,
        definition: WorkflowDefinition,
        *,
        parent: DraftRevision | None,
    ) -> DraftRevision:
        # Identity, workspace, secret and size gates all run *before* the
        # record exists, so a refused write leaves history untouched.
        if parent is not None and definition.id != parent.workflow_id:
            raise DraftIdentityError(
                f"definition id {definition.id!r} does not match workflow "
                f"{parent.workflow_id!r}"
            )
        if parent is not None and definition.workspace_id != parent.workspace_id:
            raise DraftWorkspaceError(
                f"revision of {parent.workflow_id!r} would move workspace "
                f"{parent.workspace_id!r} to {definition.workspace_id!r}"
            )

        assert_no_secret_config(definition)
        enforce_bounds(canonical_bytes(definition), limit=MAX_PAYLOAD_BYTES)

        revisions = self._history.get(definition.id, [])
        if parent is not None:
            # Defensive: the caller must pass the true head.
            if revisions and parent.number != revisions[-1].number:
                raise DraftConflictError(
                    f"parent revision {parent.number} is not the head "
                    f"({revisions[-1].number}) of {definition.id!r}"
                )
        existing_head = revisions[-1] if revisions else None
        head_hash = WorkflowRevision.create(definition).content_hash

        if existing_head is not None and existing_head.content_hash == head_hash:
            # Idempotent: same content, same revision. No history growth.
            return existing_head

        next_number = (existing_head.number + 1) if existing_head else 1
        stored = WorkflowRevision.create(
            definition,
            revision=next_number,
            created_at=datetime.now(UTC).isoformat(),
        )
        record = DraftRevision(
            revision=stored,
            summary=build_summary(definition, description=definition.description),
            parent_revision=existing_head.number if existing_head else None,
            parent_hash=existing_head.content_hash if existing_head else None,
        )
        # Store a copy: the caller may keep mutating its own object.
        revisions.append(record.model_copy(deep=True))
        self._history[definition.id] = revisions
        return record.model_copy(deep=True)

    # -- public API ---------------------------------------------------------------- #

    async def save_draft(self, definition: WorkflowDefinition) -> DraftRevision:
        revisions = self._history.get(definition.id, [])
        if revisions:
            raise DraftConflictError(
                f"workflow {definition.id!r} already has {len(revisions)} "
                "revision(s); use revise() to append another"
            )
        return await self._append(definition, parent=None)

    async def revise(
        self, workflow_id: str, definition: WorkflowDefinition
    ) -> DraftRevision:
        revisions = self._history.get(workflow_id, [])
        if not revisions:
            # Unknown workflow: initialise it as revision 1.
            return await self._append(definition, parent=None)
        if definition.id != workflow_id:
            raise DraftIdentityError(
                f"definition id {definition.id!r} does not match workflow "
                f"{workflow_id!r}"
            )
        return await self._append(definition, parent=revisions[-1])

    async def get(self, workflow_id: str, revision: int) -> DraftRevision:
        revisions = self._history.get(workflow_id)
        if not revisions:
            raise DraftNotFoundError(f"unknown workflow {workflow_id!r}")
        for record in revisions:
            if record.number == revision:
                return record.model_copy(deep=True)
        raise DraftNotFoundError(
            f"workflow {workflow_id!r} has no revision {revision} "
            f"(has {revisions[0].number}..{revisions[-1].number})"
        )

    async def latest(self, workflow_id: str) -> DraftRevision | None:
        revisions = self._history.get(workflow_id)
        if not revisions:
            return None
        return revisions[-1].model_copy(deep=True)

    async def history(self, workflow_id: str) -> list[DraftRevision]:
        return [r.model_copy(deep=True) for r in self._history.get(workflow_id, [])]

    async def count(self, workflow_id: str) -> int:
        return len(self._history.get(workflow_id, []))

    async def workflow_ids(self) -> list[str]:
        return sorted(self._history)


__all__ = [
    "DraftConflictError",
    "DraftIdentityError",
    "DraftNotFoundError",
    "DraftRevision",
    "DraftRevisionError",
    "DraftStore",
    "DraftWorkspaceError",
    "InMemoryDraftStore",
    "assert_append_only",
    "verify_revision",
]
