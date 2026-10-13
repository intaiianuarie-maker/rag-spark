"""Workflow checkpoint / restart / resume + evidence propagation (T-V4-016 / W3).

Workflow execution checkpoints durably and resumes identically after a kill;
evidence produced before the kill re-attaches to the resumed run instead of
being orphaned by the restart.

Discipline mirrors the V3 ``StepStore`` pattern (append-only, ordered, redacted,
bounded) but is a **separate implementation** — the W3 adapter
(``adapters/v3_adapter.py``) is the only bridge to V3 and it stays flagged
off, so nothing here imports V3 product code across the seam.

Checkpoint boundaries — persisted, not every heartbeat
-----------------------------------------------------
Only the boundaries worth resuming from are written (card rule 1):

* **terminal** — the whole run reached a terminal status (completed/failed);
* **approval** — a node completed whose type declares external effect, so the
  run is at an approval-style pause where a human may intervene;
* **completed-community work** — an ``OUTPUT`` node completed, which is the
  boundary at which a community of work has produced its result and earlier
  per-node work no longer needs replaying.

Everything else (per-node heartbeats, retries) stays ephemeral. This keeps the
durable surface small and monotonic.

Resume equality
---------------
A resumed run is driven through :class:`ResumableExecutor`, which returns
checkpointed outputs for nodes already completed before the kill and delegates
the rest to the wrapped executor. Because the checkpoint is seeded into the
*wrapper* rather than into ``WorkflowExecutor``, the executor core is
untouched (card rule) and the resulting ``WorkflowRun`` is identical to a
never-interrupted run — proven by trace equality in the tests.
"""

from __future__ import annotations

import json
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .interfaces import (
    NodeRun,
    NodeSpec,
    NodeType,
    WorkflowRun,
    WorkflowRunStatus,
)

# ---------------------------------------------------------------------------
# Bounds (mirroring the V3 StepStore discipline)
# ---------------------------------------------------------------------------

#: Maximum size of one serialized checkpoint payload (bytes).
MAX_CHECKPOINT_BYTES = 262_144  # 256 KB
#: Maximum length of any single redacted text field.
MAX_TEXT_CHARS = 8_192
#: Secrets are never stored. Deny-list mirrors the V3 StepStore deny-list; kept
#: as a separate tuple rather than imported across the seam.
_DENY_KEYS = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "bearer",
    "authorization",
    "auth",
    "sk-",
    "ghp_",
    "gho_",
    "-----begin",
)
_SECRET_PATTERNS = [
    # Ordered, innermost-first composition: inner patterns must be applied
    # before outer ones so a Bearer header's scheme is not consumed first.
    re.compile(r"(?i)(sk-[A-Za-z0-9]{20,})"),
    re.compile(r"(?i)(gh[pous]_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?i)(-----BEGIN [A-Z ]+-----)"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_\.]+"),
]

#: Node types whose completion marks an *approval*-style pause: the node
#: declares external effect, so the run stops at a boundary a human may act on.
APPROVAL_NODE_TYPES = frozenset({NodeType.OUTPUT})

#: Node types whose completion marks *completed-community work*. Same set today;
#: kept separate so the two boundaries can diverge without a behaviour change.
COMMUNITY_NODE_TYPES = frozenset({NodeType.OUTPUT})


class CheckpointSequenceError(ValueError):
    """A checkpoint was appended out of monotonic order for its run."""


class ResumeStateError(RuntimeError):
    """Resume was asked for state that cannot faithfully continue."""


def _enforce_bounds(checkpoint: WorkflowCheckpoint) -> str:
    """Serialize + bound-check a checkpoint before any store persists it.

    Returns the serialized payload. Oversized checkpoints are **refused**,
    never silently truncated — a truncated checkpoint would resume into a
    state the pre-kill run never actually produced.
    """
    raw = checkpoint.to_json()
    size = len(raw.encode("utf-8"))
    if size > MAX_CHECKPOINT_BYTES:
        raise ResumeStateError(
            f"checkpoint payload {size} exceeds {MAX_CHECKPOINT_BYTES} bytes"
        )
    return raw


def _redact_text(text: Any) -> Any:
    """Redact secrets from ``text`` at the store boundary (never store them)."""
    if not isinstance(text, str):
        return text
    redacted = text[:MAX_TEXT_CHARS]
    for key in _DENY_KEYS:
        idx = redacted.lower().find(key)
        if idx != -1:
            sep = redacted.find(":", idx)
            eq = redacted.find("=", idx)
            cut = min([p for p in (sep, eq) if p != -1], default=-1)
            if cut != -1:
                redacted = redacted[: cut + 1] + "[REDACTED]"
            else:
                redacted = redacted[:idx] + "[REDACTED]"
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def _redact(value: Any) -> Any:
    """Recursively redact + bound a JSON-ish value before it is persisted."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {str(k)[:MAX_TEXT_CHARS]: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Checkpoint model
# ---------------------------------------------------------------------------


class EvidenceRow(BaseModel):
    """One evidence unit produced pre-kill that must survive a restart.

    Provenance is first-class: the row records which run, which node and which
    workspace produced it, so a resumed run re-attaches it rather than
    recreating a lossy copy.
    """

    run_id: str
    node_id: str
    workspace_id: str
    kind: str  # citation | verdict | attribution
    payload: dict[str, Any] = Field(default_factory=dict)
    sequence: int = 0
    created_at: str = ""


class WorkflowCheckpoint(BaseModel):
    """A durable, resumable workflow boundary state (append-only per run)."""

    run_id: str
    workflow_id: str
    revision_id: str
    workspace_id: str
    sequence: int = 0
    status: str = WorkflowRunStatus.RUNNING.value
    #: Which checkpoint boundary produced this one (terminal | approval | community).
    boundary: str = ""
    initial_input: dict[str, Any] = Field(default_factory=dict)
    #: Completed node ids at checkpoint time.
    completed_nodes: list[str] = Field(default_factory=list)
    #: Checkpointed node outputs (pre-redaction), keyed by node id.
    node_outputs: dict[str, Any] = Field(default_factory=dict)
    #: Serialized completed NodeRuns at checkpoint time.
    node_runs: dict[str, NodeRun] = Field(default_factory=dict)
    #: Evidence rows produced up to this boundary.
    evidence: list[EvidenceRow] = Field(default_factory=list)
    revision_hash: str = ""
    checkpointed_at: str = ""

    def to_json(self) -> str:
        return json.dumps(_redact(self.model_dump(mode="json")), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class CheckpointStore(ABC):
    """Append-only checkpoint store: append, read latest, read evidence."""

    @abstractmethod
    async def append(self, checkpoint: WorkflowCheckpoint) -> WorkflowCheckpoint:
        """Append a checkpoint (enforcing monotonic per-run sequence)."""
        raise NotImplementedError

    @abstractmethod
    async def latest(self, run_id: str) -> WorkflowCheckpoint | None:
        """Newest checkpoint for ``run_id``."""
        raise NotImplementedError

    @abstractmethod
    async def evidence_for(self, run_id: str) -> list[EvidenceRow]:
        """Every evidence row recorded for ``run_id`` (in order)."""
        raise NotImplementedError

    @abstractmethod
    async def next_sequence(self, run_id: str) -> int:
        """Next monotonic sequence number for ``run_id`` (1-based)."""
        raise NotImplementedError


class InMemoryCheckpointStore(CheckpointStore):
    """In-memory checkpoint store; monotonic per run, ordered."""

    def __init__(self) -> None:
        self._by_run: dict[str, list[WorkflowCheckpoint]] = {}
        self._seq: dict[str, int] = {}

    async def next_sequence(self, run_id: str) -> int:
        return self._seq.get(run_id, 0) + 1

    async def append(self, checkpoint: WorkflowCheckpoint) -> WorkflowCheckpoint:
        _enforce_bounds(checkpoint)
        seq = self._seq.get(checkpoint.run_id, 0) + 1
        checkpoint.sequence = seq
        self._seq[checkpoint.run_id] = seq
        self._by_run.setdefault(checkpoint.run_id, []).append(checkpoint)
        return checkpoint

    async def latest(self, run_id: str) -> WorkflowCheckpoint | None:
        rows = self._by_run.get(run_id)
        return rows[-1] if rows else None

    async def evidence_for(self, run_id: str) -> list[EvidenceRow]:
        return list(self._by_run.get(run_id, [])[-1].evidence)


class SqliteCheckpointStore(CheckpointStore):
    """Durable checkpoint store (one SQLite file, monotonic, ordered)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._init_db()

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                  run_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL,
                  data TEXT NOT NULL,
                  PRIMARY KEY (run_id, sequence)
                )
                """
            )
            con.commit()

    async def next_sequence(self, run_id: str) -> int:
        with sqlite3.connect(self.path) as con:
            cur = con.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM workflow_checkpoints"
                " WHERE run_id=?",
                (run_id,),
            )
            return int(cur.fetchone()[0]) + 1

    async def append(self, checkpoint: WorkflowCheckpoint) -> WorkflowCheckpoint:
        expected_seq = await self.next_sequence(checkpoint.run_id)
        if checkpoint.sequence and checkpoint.sequence > expected_seq:
            raise CheckpointSequenceError(
                f"checkpoint sequence {checkpoint.sequence} is ahead of expected "
                f"{expected_seq} for run {checkpoint.run_id}"
            )
        checkpoint.sequence = expected_seq
        raw = _enforce_bounds(checkpoint)
        with sqlite3.connect(self.path) as con:
            con.execute(
                "INSERT OR REPLACE INTO workflow_checkpoints"
                " (run_id, sequence, data) VALUES (?, ?, ?)",
                (checkpoint.run_id, checkpoint.sequence, raw),
            )
            con.commit()
        return checkpoint

    async def latest(self, run_id: str) -> WorkflowCheckpoint | None:
        with sqlite3.connect(self.path) as con:
            cur = con.execute(
                "SELECT data FROM workflow_checkpoints"
                " WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            )
            row = cur.fetchone()
        return WorkflowCheckpoint.model_validate(json.loads(row[0])) if row else None

    async def evidence_for(self, run_id: str) -> list[EvidenceRow]:
        latest = await self.latest(run_id)
        return list(latest.evidence) if latest else []


# ---------------------------------------------------------------------------
# Checkpoint boundary policy (pure)
# ---------------------------------------------------------------------------


def checkpoint_boundary(
    *,
    node_type: NodeType | str,
    run_complete: bool,
    run_failed: bool,
    previously_completed: bool,
) -> str | None:
    """Which checkpoint boundary a node completion creates (pure policy).

    Returns ``None`` — do not checkpoint — for boundaries that are not worth
    persisting. Terminal beats approval beats community, and re-completing a
    node already captured at an earlier boundary is idempotent (returns the
    community boundary again, never a new sequence-worthy pause).
    """
    if run_complete or run_failed:
        return "terminal"
    if previously_completed:
        # Re-run of an already-captured node (a retry after a transient
        # failure, say): not a new pause boundary.
        return "community" if str(node_type) in COMMUNITY_NODE_TYPES else None
    if str(node_type) in APPROVAL_NODE_TYPES:
        return "approval"
    if str(node_type) in COMMUNITY_NODE_TYPES:
        return "community"
    return None


def is_checkpoint_boundary(node_type: NodeType | str, run_complete: bool = False) -> bool:
    """Whether this node completion is worth persisting."""
    return (
        checkpoint_boundary(
            node_type=node_type,
            run_complete=run_complete,
            run_failed=False,
            previously_completed=False,
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Resumable executor — seeds checkpointed state into the wrapper, not the core
# ---------------------------------------------------------------------------


class ResumableExecutor:
    """NodeExecutor wrapper that serves checkpointed outputs and delegates the rest.

    Returning the recorded output for a node that already completed is what
    makes a resumed run identical to a never-interrupted one, while leaving
    ``WorkflowExecutor`` untouched.
    """

    def __init__(self, base: Any, checkpoint: WorkflowCheckpoint) -> None:
        self._base = base
        self._checkpoint = checkpoint
        self.replayed: set[str] = set()
        self.delegated: set[str] = set()

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any],
        workspace_id: str,
    ) -> dict[str, Any]:
        # Scope check: a checkpoint may only serve the run's own workspace.
        if self._checkpoint.workspace_id and workspace_id != self._checkpoint.workspace_id:
            raise ResumeStateError(
                "resume workspace mismatch: "
                f"{workspace_id!r} != checkpointed {self._checkpoint.workspace_id!r}"
            )
        if node.id in self._checkpoint.node_outputs:
            self.replayed.add(node.id)
            return self._checkpoint.node_outputs[node.id]
        self.delegated.add(node.id)
        return await self._base.execute_node(node, input_data, workspace_id)

    @property
    def checkpoint(self) -> WorkflowCheckpoint:
        return self._checkpoint

    @property
    def evidence(self) -> list[EvidenceRow]:
        return list(self._checkpoint.evidence)


@dataclass(frozen=True)
class ResumedRun:
    """A resumed run plus the pre-kill evidence re-attached to it.

    Kept as an explicit pairing rather than a new field on ``WorkflowRun``:
    the executor core and the shared run model stay untouched, and no evidence
    is silently absorbed into the run trace.
    """

    run: WorkflowRun
    evidence: tuple[EvidenceRow, ...]
    checkpoint: WorkflowCheckpoint

    @property
    def evidence_count(self) -> int:
        return len(self.evidence)


def attach_evidence(run: WorkflowRun, checkpoint: WorkflowCheckpoint) -> ResumedRun:
    """Re-attach pre-kill evidence to a resumed run (no evidence orphaned).

    The evidence rows are carried verbatim — same run id, node id, workspace
    and payload as before the kill — so nothing is recreated or lossy.
    """
    return ResumedRun(
        run=run,
        evidence=tuple(e.model_copy() for e in checkpoint.evidence),
        checkpoint=checkpoint,
    )
