"""Durable workflow revision draft store and approval binding enforcement (T-R18 / Phase 4).

Enforces:
1. Append-only persistence at the storage layer:
   - UPDATE and DELETE are prohibited via triggers (trg_wdr_no_update, trg_wdr_no_delete).
   - Monotonic revision allocation is enforced transactionally and at the trigger layer
     (trg_wdr_monotonic_insert), blocking backwards and out-of-order inserts (F13 remediation).
2. Workspace-isolated queries:
   - Every read and write query is strictly scoped by workspace_id.
3. Verify-on-read:
   - Every read operation (get, latest, history) re-verifies the canonical SHA-256
     content_hash of the stored spec, raising DraftTamperError on any modification.
4. Approval bindings:
   - Immutably binds workspace_id + content_hash + policy_revision.
   - Refuses bindings missing required hash or policy.
   - Re-checks on execution to prevent execution under swapped definitions or bumped policies.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from workflow_engine.draft_store import (
    DraftConflictError,
    DraftIdentityError,
    DraftNotFoundError,
    DraftRevision,
    DraftRevisionError,
    DraftStore,
    DraftWorkspaceError,
)
from workflow_engine.interfaces import WorkflowDefinition, WorkflowRevision
from workflow_engine.store_discipline import (
    MAX_PAYLOAD_BYTES,
    assert_no_secret_config,
    build_summary,
    canonical_bytes,
    enforce_bounds,
)


class DraftMutationError(DraftRevisionError):
    """Raised when an UPDATE, DELETE, or in-place modification is attempted."""


class DraftTamperError(DraftRevisionError):
    """Raised when verify-on-read detects a content_hash mismatch (tamper detected)."""


class ApprovalBindingError(RuntimeError):
    """Raised when an approval binding is invalid or missing required fields."""


MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1] / "workflow_engine" / "migrations"
)
UP_SQL = MIGRATIONS_DIR / "0001_workflow_draft_revisions.up.sql"


def _get_wdr_ddl() -> str:
    return UP_SQL.read_text(encoding="utf-8")


class SqliteDraftStore(DraftStore):
    """Durable SQLite implementation of append-only draft revision store with verify-on-read."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._con = sqlite3.connect(self.db_path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._con.executescript(_get_wdr_ddl())
        self._con.commit()

    # -- Forbidden mutation interfaces (F13 defense) ------------------------------ #

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise DraftMutationError("draft store is append-only: update is prohibited")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise DraftMutationError("draft store is append-only: delete is prohibited")

    def remove(self, *args: Any, **kwargs: Any) -> None:
        raise DraftMutationError("draft store is append-only: remove is prohibited")

    def overwrite(self, *args: Any, **kwargs: Any) -> None:
        raise DraftMutationError("draft store is append-only: overwrite is prohibited")

    def purge(self, *args: Any, **kwargs: Any) -> None:
        raise DraftMutationError("draft store is append-only: purge is prohibited")

    # -- Internal write helper --------------------------------------------------- #

    def _insert_revision(
        self,
        definition: WorkflowDefinition,
        revision_num: int,
        parent: DraftRevision | None,
    ) -> DraftRevision:
        assert_no_secret_config(definition)
        enforce_bounds(canonical_bytes(definition), limit=MAX_PAYLOAD_BYTES)

        # Canonicalize spec bytes for persistent storage
        payload = WorkflowRevision._canonical_payload(definition.model_dump())
        canonical_spec = json.dumps(payload, sort_keys=True, default=str)

        stored_rev = WorkflowRevision.create(
            definition,
            revision=revision_num,
            created_at=datetime.now(UTC).isoformat(),
        )
        summary = build_summary(definition, description=definition.description)
        parent_rev = parent.number if parent is not None else None
        parent_hash = parent.content_hash if parent is not None else None

        try:
            self._con.execute(
                """
                INSERT INTO workflow_draft_revisions (
                    workspace_id, workflow_id, revision, content_hash, spec, summary,
                    parent_revision, parent_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    definition.workspace_id,
                    definition.id,
                    stored_rev.revision,
                    stored_rev.content_hash,
                    canonical_spec,
                    summary,
                    parent_rev,
                    parent_hash,
                    stored_rev.created_at,
                ),
            )
            self._con.commit()
        except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
            self._con.rollback()
            raise DraftConflictError(
                f"storage rejected revision insert for workflow {definition.id!r}: {exc}"
            ) from exc

        return DraftRevision(
            revision=stored_rev,
            summary=summary,
            parent_revision=parent_rev,
            parent_hash=parent_hash,
        )

    # -- Public DraftStore ABC implementations ----------------------------------- #

    async def save_draft(self, definition: WorkflowDefinition) -> DraftRevision:
        cur = self._con.execute(
            """
            SELECT COUNT(*) AS cnt FROM workflow_draft_revisions
            WHERE workspace_id = ? AND workflow_id = ?
            """,
            (definition.workspace_id, definition.id),
        )
        row = cur.fetchone()
        if row and row["cnt"] > 0:
            raise DraftConflictError(
                f"workflow {definition.id!r} in workspace {definition.workspace_id!r} "
                f"already has {row['cnt']} revision(s); use revise() to append another"
            )
        return self._insert_revision(definition, revision_num=1, parent=None)

    async def revise(
        self,
        workspace_id: str,
        workflow_id: str,
        definition: WorkflowDefinition,
    ) -> DraftRevision:
        if definition.workspace_id != workspace_id:
            raise DraftWorkspaceError(
                f"definition workspace {definition.workspace_id!r} "
                f"does not match scope {workspace_id!r}"
            )
        if definition.id != workflow_id:
            raise DraftIdentityError(
                f"definition id {definition.id!r} does not match workflow {workflow_id!r}"
            )

        # Transactional head lookup
        cur = self._con.execute(
            """
            SELECT revision, content_hash FROM workflow_draft_revisions
            WHERE workspace_id = ? AND workflow_id = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (workspace_id, workflow_id),
        )
        head_row = cur.fetchone()
        if head_row is None:
            return self._insert_revision(definition, revision_num=1, parent=None)

        current_hash = WorkflowRevision._compute_content_hash(definition)
        if head_row["content_hash"] == current_hash:
            # Idempotent: same content, return current head
            latest_rec = await self.get(workspace_id, workflow_id, head_row["revision"])
            return latest_rec

        parent = await self.get(workspace_id, workflow_id, head_row["revision"])
        next_revision = head_row["revision"] + 1
        return self._insert_revision(definition, revision_num=next_revision, parent=parent)

    # -- Verify-on-Read implementations (Metric 2) ------------------------------- #

    def _row_to_record_with_hash_verification(self, row: sqlite3.Row) -> DraftRevision:
        """Hydrates a DraftRevision while asserting canonical SHA-256 hash integrity.

        Raises DraftTamperError immediately if data was tampered with in storage.
        """
        stored_hash: str = row["content_hash"]
        spec_raw: str = row["spec"]

        try:
            definition = WorkflowDefinition.model_validate_json(spec_raw)
        except Exception as exc:
            raise DraftTamperError(
                f"stored spec JSON for {row['workflow_id']} rev {row['revision']} "
                f"is unparseable: {exc}"
            ) from exc

        computed_hash = WorkflowRevision._compute_content_hash(definition)
        if computed_hash != stored_hash:
            raise DraftTamperError(
                f"tamper detected: content_hash mismatch on workflow {row['workflow_id']!r} "
                f"revision {row['revision']} in workspace {row['workspace_id']!r}: "
                f"stored {stored_hash}, computed {computed_hash}"
            )

        revision = WorkflowRevision(
            workflow_id=row["workflow_id"],
            revision=row["revision"],
            content_hash=stored_hash,
            spec=definition,
            created_at=row["created_at"],
        )
        return DraftRevision(
            revision=revision,
            summary=row["summary"],
            parent_revision=row["parent_revision"],
            parent_hash=row["parent_hash"],
        )

    async def get(
        self,
        workspace_id: str,
        workflow_id: str,
        revision: int,
    ) -> DraftRevision:
        cur = self._con.execute(
            """
            SELECT workspace_id, workflow_id, revision, content_hash, spec, summary,
                   parent_revision, parent_hash, created_at
            FROM workflow_draft_revisions
            WHERE workspace_id = ? AND workflow_id = ? AND revision = ?
            """,
            (workspace_id, workflow_id, revision),
        )
        row = cur.fetchone()
        if row is None:
            raise DraftNotFoundError(
                f"draft {workflow_id!r} revision {revision} not found in workspace {workspace_id!r}"
            )
        return self._row_to_record_with_hash_verification(row)

    async def latest(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> DraftRevision | None:
        cur = self._con.execute(
            """
            SELECT workspace_id, workflow_id, revision, content_hash, spec, summary,
                   parent_revision, parent_hash, created_at
            FROM workflow_draft_revisions
            WHERE workspace_id = ? AND workflow_id = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (workspace_id, workflow_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_record_with_hash_verification(row)

    async def history(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> list[DraftRevision]:
        cur = self._con.execute(
            """
            SELECT workspace_id, workflow_id, revision, content_hash, spec, summary,
                   parent_revision, parent_hash, created_at
            FROM workflow_draft_revisions
            WHERE workspace_id = ? AND workflow_id = ?
            ORDER BY revision ASC
            """,
            (workspace_id, workflow_id),
        )
        rows = cur.fetchall()
        return [self._row_to_record_with_hash_verification(r) for r in rows]

    async def count(self, workspace_id: str, workflow_id: str) -> int:
        cur = self._con.execute(
            """
            SELECT COUNT(*) AS cnt FROM workflow_draft_revisions
            WHERE workspace_id = ? AND workflow_id = ?
            """,
            (workspace_id, workflow_id),
        )
        row = cur.fetchone()
        return int(row["cnt"]) if row else 0

    async def workflow_ids(self, workspace_id: str) -> list[str]:
        cur = self._con.execute(
            """
            SELECT DISTINCT workflow_id FROM workflow_draft_revisions
            WHERE workspace_id = ?
            ORDER BY workflow_id ASC
            """,
            (workspace_id,),
        )
        return [str(r["workflow_id"]) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Approval Bindings (Metric 3): Workspace + Hash + Policy
# --------------------------------------------------------------------------- #


class WorkflowApprovalBinding(BaseModel):
    """Immutable binding uniting workspace, content_hash, and policy_revision."""

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    workflow_id: str
    revision: int
    content_hash: str
    policy_revision: str
    approver_id: str
    status: str = "approved"
    approved_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="after")
    def _validate_binding_integrity(self) -> WorkflowApprovalBinding:
        if not self.workspace_id or not self.workspace_id.strip():
            raise ApprovalBindingError("approval binding missing required workspace_id")
        if not self.workflow_id or not self.workflow_id.strip():
            raise ApprovalBindingError("approval binding missing required workflow_id")
        if self.revision < 1:
            raise ApprovalBindingError("approval binding revision must be >= 1")
        if not self.content_hash or len(self.content_hash) != 64:
            raise ApprovalBindingError(
                f"approval binding content_hash must be 64-char hex, got {self.content_hash!r}"
            )
        if not self.policy_revision or not self.policy_revision.strip():
            raise ApprovalBindingError("approval binding missing required policy_revision")
        if not self.approver_id or not self.approver_id.strip():
            raise ApprovalBindingError("approval binding missing required approver_id")
        return self


class WorkflowApprovalStore(ABC):
    """Interface for managing immutable workflow approval bindings."""

    @abstractmethod
    async def bind_approval(
        self,
        *,
        workspace_id: str,
        workflow_id: str,
        revision: int,
        content_hash: str,
        policy_revision: str,
        approver_id: str,
        status: str = "approved",
    ) -> WorkflowApprovalBinding:
        """Create and persist an approval binding carrying workspace + hash + policy."""
        raise NotImplementedError

    @abstractmethod
    async def is_approved(
        self,
        *,
        workspace_id: str,
        workflow_id: str,
        revision: int,
        content_hash: str,
        policy_revision: str,
    ) -> bool:
        """Check if an approval binding exists matching exact workspace, hash, and policy."""
        raise NotImplementedError

    @abstractmethod
    async def get_binding(
        self,
        *,
        workspace_id: str,
        workflow_id: str,
        revision: int,
        policy_revision: str | None = None,
    ) -> WorkflowApprovalBinding | None:
        """Fetch active binding for a given revision."""
        raise NotImplementedError

    @abstractmethod
    async def list_bindings(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> list[WorkflowApprovalBinding]:
        """List all approval history for a workflow in a workspace."""
        raise NotImplementedError


class InMemoryWorkflowApprovalStore(WorkflowApprovalStore):
    """In-memory implementation of WorkflowApprovalStore for tests."""

    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str, int, str], WorkflowApprovalBinding] = {}

    async def bind_approval(
        self,
        *,
        workspace_id: str,
        workflow_id: str,
        revision: int,
        content_hash: str,
        policy_revision: str,
        approver_id: str,
        status: str = "approved",
    ) -> WorkflowApprovalBinding:
        binding = WorkflowApprovalBinding(
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            revision=revision,
            content_hash=content_hash,
            policy_revision=policy_revision,
            approver_id=approver_id,
            status=status,
        )
        key = (workspace_id, workflow_id, revision, policy_revision)
        self._bindings[key] = binding
        return binding

    async def is_approved(
        self,
        *,
        workspace_id: str,
        workflow_id: str,
        revision: int,
        content_hash: str,
        policy_revision: str,
    ) -> bool:
        key = (workspace_id, workflow_id, revision, policy_revision)
        binding = self._bindings.get(key)
        if binding is None:
            return False
        return (
            binding.workspace_id == workspace_id
            and binding.content_hash == content_hash
            and binding.policy_revision == policy_revision
            and binding.status == "approved"
        )

    async def get_binding(
        self,
        *,
        workspace_id: str,
        workflow_id: str,
        revision: int,
        policy_revision: str | None = None,
    ) -> WorkflowApprovalBinding | None:
        for (ws, wf, rev, pol), binding in self._bindings.items():
            if ws == workspace_id and wf == workflow_id and rev == revision:
                if policy_revision is None or pol == policy_revision:
                    return binding
        return None

    async def list_bindings(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> list[WorkflowApprovalBinding]:
        return [
            b
            for (ws, wf, _, _), b in self._bindings.items()
            if ws == workspace_id and wf == workflow_id
        ]


class SqliteWorkflowApprovalStore(WorkflowApprovalStore):
    """Persistent SQLite approval store enforcing immutability and workspace+hash+policy binding."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._con = sqlite3.connect(self.db_path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(_get_wdr_ddl())
        self._con.commit()

    async def bind_approval(
        self,
        *,
        workspace_id: str,
        workflow_id: str,
        revision: int,
        content_hash: str,
        policy_revision: str,
        approver_id: str,
        status: str = "approved",
    ) -> WorkflowApprovalBinding:
        binding = WorkflowApprovalBinding(
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            revision=revision,
            content_hash=content_hash,
            policy_revision=policy_revision,
            approver_id=approver_id,
            status=status,
        )
        try:
            self._con.execute(
                """
                INSERT INTO workflow_approval_bindings (
                    workspace_id, workflow_id, revision, content_hash, policy_revision,
                    approver_id, status, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.workspace_id,
                    binding.workflow_id,
                    binding.revision,
                    binding.content_hash,
                    binding.policy_revision,
                    binding.approver_id,
                    binding.status,
                    binding.approved_at,
                ),
            )
            self._con.commit()
        except sqlite3.IntegrityError as exc:
            self._con.rollback()
            raise ApprovalBindingError(
                f"approval binding already exists or constraint failed: {exc}"
            ) from exc
        return binding

    async def is_approved(
        self,
        *,
        workspace_id: str,
        workflow_id: str,
        revision: int,
        content_hash: str,
        policy_revision: str,
    ) -> bool:
        cur = self._con.execute(
            """
            SELECT COUNT(*) AS cnt FROM workflow_approval_bindings
            WHERE workspace_id = ?
              AND workflow_id = ?
              AND revision = ?
              AND content_hash = ?
              AND policy_revision = ?
              AND status = 'approved'
            """,
            (workspace_id, workflow_id, revision, content_hash, policy_revision),
        )
        row = cur.fetchone()
        return bool(row and row["cnt"] > 0)

    async def get_binding(
        self,
        *,
        workspace_id: str,
        workflow_id: str,
        revision: int,
        policy_revision: str | None = None,
    ) -> WorkflowApprovalBinding | None:
        if policy_revision:
            cur = self._con.execute(
                """
                SELECT workspace_id, workflow_id, revision, content_hash, policy_revision,
                       approver_id, status, approved_at
                FROM workflow_approval_bindings
                WHERE workspace_id = ? AND workflow_id = ? AND revision = ? AND policy_revision = ?
                """,
                (workspace_id, workflow_id, revision, policy_revision),
            )
        else:
            cur = self._con.execute(
                """
                SELECT workspace_id, workflow_id, revision, content_hash, policy_revision,
                       approver_id, status, approved_at
                FROM workflow_approval_bindings
                WHERE workspace_id = ? AND workflow_id = ? AND revision = ?
                ORDER BY approved_at DESC LIMIT 1
                """,
                (workspace_id, workflow_id, revision),
            )
        row = cur.fetchone()
        if row is None:
            return None
        return WorkflowApprovalBinding(
            workspace_id=row["workspace_id"],
            workflow_id=row["workflow_id"],
            revision=row["revision"],
            content_hash=row["content_hash"],
            policy_revision=row["policy_revision"],
            approver_id=row["approver_id"],
            status=row["status"],
            approved_at=row["approved_at"],
        )

    async def list_bindings(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> list[WorkflowApprovalBinding]:
        cur = self._con.execute(
            """
            SELECT workspace_id, workflow_id, revision, content_hash, policy_revision,
                   approver_id, status, approved_at
            FROM workflow_approval_bindings
            WHERE workspace_id = ? AND workflow_id = ?
            ORDER BY revision ASC, approved_at ASC
            """,
            (workspace_id, workflow_id),
        )
        return [
            WorkflowApprovalBinding(
                workspace_id=r["workspace_id"],
                workflow_id=r["workflow_id"],
                revision=r["revision"],
                content_hash=r["content_hash"],
                policy_revision=r["policy_revision"],
                approver_id=r["approver_id"],
                status=r["status"],
                approved_at=r["approved_at"],
            )
            for r in cur.fetchall()
        ]


# --------------------------------------------------------------------------- #
# Global Seams & Factory Functions
# --------------------------------------------------------------------------- #

_DRAFT_STORE: DraftStore | None = None
_APPROVAL_STORE: WorkflowApprovalStore | None = None


def get_draft_store() -> DraftStore:
    global _DRAFT_STORE
    if _DRAFT_STORE is None:
        _DRAFT_STORE = SqliteDraftStore()
    return _DRAFT_STORE


def set_draft_store(store: DraftStore | None) -> None:
    global _DRAFT_STORE
    _DRAFT_STORE = store


def get_approval_store() -> WorkflowApprovalStore:
    global _APPROVAL_STORE
    if _APPROVAL_STORE is None:
        _APPROVAL_STORE = SqliteWorkflowApprovalStore()
    return _APPROVAL_STORE


def set_approval_store(store: WorkflowApprovalStore | None) -> None:
    global _APPROVAL_STORE
    _APPROVAL_STORE = store


def reset_workflow_stores() -> None:
    global _DRAFT_STORE, _APPROVAL_STORE
    _DRAFT_STORE = None
    _APPROVAL_STORE = None
