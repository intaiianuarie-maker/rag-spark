"""Durable storage for Agent lifecycle: Runs, Checkpoints, Steps, and Evidence (T-R15).

Provides:
- DurableRunRecord: structured metadata and execution state for an agent run.
- RunStore (ABC) with InMemoryRunStore and SqliteRunStore implementations:
  persists run identity before 202 accepted (runs accepted without pre-allocated ID = 0).
- DurableCheckpointStore: wraps StepStoreCheckpointAdapter to persist and restore
  run state, usage, context, approvals, and step history (lost-step resumptions unrecoverable = 0).
- StepStore & EvidenceStore re-exports for unified access.
- Global singleton store accessors with reset_all_stores() for test isolation.
"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.adapters.checkpoint import StepStoreCheckpointAdapter
from agents.evidence import (
    EvidenceStore,
    InMemoryEvidenceStore,
)
from agents.interfaces import (
    AgentErrorCode,
    RunContext,
    RunState,
    RunStatus,
    StepEvent,
    Usage,
)
from agents.step_store import (
    InMemoryStepStore,
    StepStore,
)
from pydantic import BaseModel, ConfigDict, Field


class DurableRunRecord(BaseModel):
    """Durable record representing an agent run lifecycle."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    workspace_id: str
    principal_id: str
    goal: str
    status: RunStatus = RunStatus.QUEUED
    error: str | None = None
    error_code: AgentErrorCode | None = None
    context: RunContext | None = None
    state: RunState = Field(default_factory=lambda: RunState(status=RunStatus.QUEUED))
    final_output: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def update_status(
        self,
        status: RunStatus,
        *,
        error: str | None = None,
        error_code: AgentErrorCode | None = None,
    ) -> None:
        """Update run record and underlying RunState monotonically."""
        if not RunState.is_terminal(self.state.status):
            self.state.transition_to(status, error_code=error_code, error_detail=error)
        self.status = self.state.status
        self.error = error or self.state.error_detail
        self.error_code = error_code or self.state.error_code
        self.updated_at = datetime.now(UTC).isoformat()


class RunStore(ABC):
    """Abstract interface for durable agent run records."""

    @abstractmethod
    def create_run(self, record: DurableRunRecord) -> DurableRunRecord:
        """Store a pre-allocated run record before 202 Accepted is returned."""
        raise NotImplementedError

    @abstractmethod
    def get_run(self, run_id: str) -> DurableRunRecord | None:
        """Fetch a run record by run_id."""
        raise NotImplementedError

    @abstractmethod
    def update_run(self, record: DurableRunRecord) -> DurableRunRecord:
        """Update an existing run record."""
        raise NotImplementedError

    @abstractmethod
    def update_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error: str | None = None,
        error_code: AgentErrorCode | None = None,
    ) -> DurableRunRecord | None:
        """Update the status of a run record."""
        raise NotImplementedError

    @abstractmethod
    def list_runs(self, workspace_id: str) -> list[DurableRunRecord]:
        """List all runs for a workspace."""
        raise NotImplementedError

    @abstractmethod
    def delete_run(self, run_id: str) -> bool:
        """Delete a run record."""
        raise NotImplementedError


class InMemoryRunStore(RunStore):
    """Thread-safe in-memory store for agent run records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, DurableRunRecord] = {}

    def create_run(self, record: DurableRunRecord) -> DurableRunRecord:
        with self._lock:
            if record.run_id in self._runs:
                raise ValueError(f"Run {record.run_id!r} already exists")
            self._runs[record.run_id] = record
            return record

    def get_run(self, run_id: str) -> DurableRunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def update_run(self, record: DurableRunRecord) -> DurableRunRecord:
        with self._lock:
            record.updated_at = datetime.now(UTC).isoformat()
            self._runs[record.run_id] = record
            return record

    def update_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error: str | None = None,
        error_code: AgentErrorCode | None = None,
    ) -> DurableRunRecord | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            record.update_status(status, error=error, error_code=error_code)
            return record

    def list_runs(self, workspace_id: str) -> list[DurableRunRecord]:
        with self._lock:
            return [r for r in self._runs.values() if r.workspace_id == workspace_id]

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            return self._runs.pop(run_id, None) is not None


class SqliteRunStore(RunStore):
    """SQLite-backed persistent store for agent run records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS durable_runs (
                    run_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    error_code TEXT,
                    context_json TEXT,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_runs_ws ON durable_runs(workspace_id)")
            con.commit()

    def create_run(self, record: DurableRunRecord) -> DurableRunRecord:
        with self._lock:
            with sqlite3.connect(self.path) as con:
                cur = con.execute(
                    "SELECT run_id FROM durable_runs WHERE run_id = ?",
                    (record.run_id,),
                )
                if cur.fetchone() is not None:
                    raise ValueError(f"Run {record.run_id!r} already exists")
                context_json = (
                    record.context.model_dump_json() if record.context is not None else None
                )
                state_json = record.state.model_dump_json()
                con.execute(
                    """
                    INSERT INTO durable_runs (
                        run_id, workspace_id, principal_id, goal, status,
                        error, error_code, context_json, state_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        record.workspace_id,
                        record.principal_id,
                        record.goal,
                        record.status.value,
                        record.error,
                        record.error_code.value if record.error_code else None,
                        context_json,
                        state_json,
                        record.created_at,
                        record.updated_at,
                    ),
                )
                con.commit()
                return record

    def _row_to_record(self, row: tuple[Any, ...]) -> DurableRunRecord:
        (
            run_id,
            workspace_id,
            principal_id,
            goal,
            status_val,
            error,
            error_code_val,
            context_json,
            state_json,
            created_at,
            updated_at,
        ) = row

        context = RunContext.model_validate_json(context_json) if context_json else None
        state = (
            RunState.model_validate_json(state_json)
            if state_json
            else RunState(status=RunStatus(status_val))
        )
        return DurableRunRecord(
            run_id=run_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            goal=goal,
            status=RunStatus(status_val),
            error=error,
            error_code=AgentErrorCode(error_code_val) if error_code_val else None,
            context=context,
            state=state,
            created_at=created_at,
            updated_at=updated_at,
        )

    def get_run(self, run_id: str) -> DurableRunRecord | None:
        with self._lock:
            with sqlite3.connect(self.path) as con:
                cur = con.execute(
                    """
                    SELECT run_id, workspace_id, principal_id, goal, status,
                           error, error_code, context_json, state_json, created_at, updated_at
                    FROM durable_runs WHERE run_id = ?
                    """,
                    (run_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return self._row_to_record(row)

    def update_run(self, record: DurableRunRecord) -> DurableRunRecord:
        with self._lock:
            record.updated_at = datetime.now(UTC).isoformat()
            context_json = (
                record.context.model_dump_json() if record.context is not None else None
            )
            state_json = record.state.model_dump_json()
            with sqlite3.connect(self.path) as con:
                con.execute(
                    """
                    UPDATE durable_runs SET
                        status = ?, error = ?, error_code = ?, context_json = ?,
                        state_json = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        record.status.value,
                        record.error,
                        record.error_code.value if record.error_code else None,
                        context_json,
                        state_json,
                        record.updated_at,
                        record.run_id,
                    ),
                )
                con.commit()
                return record

    def update_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error: str | None = None,
        error_code: AgentErrorCode | None = None,
    ) -> DurableRunRecord | None:
        record = self.get_run(run_id)
        if record is None:
            return None
        record.update_status(status, error=error, error_code=error_code)
        return self.update_run(record)

    def list_runs(self, workspace_id: str) -> list[DurableRunRecord]:
        with self._lock:
            with sqlite3.connect(self.path) as con:
                cur = con.execute(
                    """
                    SELECT run_id, workspace_id, principal_id, goal, status,
                           error, error_code, context_json, state_json, created_at, updated_at
                    FROM durable_runs WHERE workspace_id = ? ORDER BY created_at DESC
                    """,
                    (workspace_id,),
                )
                rows = cur.fetchall()
                return [self._row_to_record(r) for r in rows]

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            with sqlite3.connect(self.path) as con:
                cur = con.execute("DELETE FROM durable_runs WHERE run_id = ?", (run_id,))
                con.commit()
                return cur.rowcount > 0


class DurableCheckpointStore:
    """Checkpoint store bridging to StepStore for byte-identical resumption."""

    def __init__(self, step_store: StepStore | None = None) -> None:
        self._step_store = step_store if step_store is not None else InMemoryStepStore()
        self._adapter = StepStoreCheckpointAdapter(self._step_store)

    @property
    def step_store(self) -> StepStore:
        return self._step_store

    @property
    def adapter(self) -> StepStoreCheckpointAdapter:
        return self._adapter

    def save_checkpoint(
        self,
        run_id: str,
        state: RunState,
        usage: Usage,
        context: RunContext,
        approvals: dict[tuple[str, str, str], str] | None = None,
        waiting: tuple[str, str, str, str] | None = None,
        agent_run: object | None = None,
    ) -> StepEvent:
        """Persist a run checkpoint through the StepStore bridge."""
        return self._adapter.save_run_checkpoint(
            run_id=run_id,
            state=state,
            usage=usage,
            context=context,
            approvals=approvals,
            waiting=waiting,
            agent_run=agent_run,
        )

    def load_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        """Load the latest checkpoint for run_id with complete reconstructed state."""
        return self._adapter.load_run_checkpoint(run_id)

    def list_checkpoints(self, run_id: str) -> list[StepEvent]:
        """List checkpoint events for run_id."""
        return [
            ev for ev in self._step_store.list(run_id)
            if ev.action_summary.startswith("checkpoint:")
        ]


# ---------------------------------------------------------------------------
# Global Store Singletons & Resets
# ---------------------------------------------------------------------------

_GLOBAL_RUN_STORE: RunStore | None = None
_GLOBAL_STEP_STORE: StepStore | None = None
_GLOBAL_CHECKPOINT_STORE: DurableCheckpointStore | None = None
_GLOBAL_EVIDENCE_STORE: EvidenceStore | None = None
_GLOBAL_QUEUE: Any | None = None


def get_run_store() -> RunStore:
    global _GLOBAL_RUN_STORE
    if _GLOBAL_RUN_STORE is None:
        _GLOBAL_RUN_STORE = InMemoryRunStore()
    return _GLOBAL_RUN_STORE


def set_run_store(store: RunStore) -> None:
    global _GLOBAL_RUN_STORE
    _GLOBAL_RUN_STORE = store


def get_step_store() -> StepStore:
    global _GLOBAL_STEP_STORE
    if _GLOBAL_STEP_STORE is None:
        _GLOBAL_STEP_STORE = InMemoryStepStore()
    return _GLOBAL_STEP_STORE


def set_step_store(store: StepStore) -> None:
    global _GLOBAL_STEP_STORE
    _GLOBAL_STEP_STORE = store


def get_checkpoint_store() -> DurableCheckpointStore:
    global _GLOBAL_CHECKPOINT_STORE
    if _GLOBAL_CHECKPOINT_STORE is None:
        _GLOBAL_CHECKPOINT_STORE = DurableCheckpointStore(get_step_store())
    return _GLOBAL_CHECKPOINT_STORE


def set_checkpoint_store(store: DurableCheckpointStore) -> None:
    global _GLOBAL_CHECKPOINT_STORE
    _GLOBAL_CHECKPOINT_STORE = store


def get_evidence_store() -> EvidenceStore:
    global _GLOBAL_EVIDENCE_STORE
    if _GLOBAL_EVIDENCE_STORE is None:
        _GLOBAL_EVIDENCE_STORE = InMemoryEvidenceStore()
    return _GLOBAL_EVIDENCE_STORE


def set_evidence_store(store: EvidenceStore) -> None:
    global _GLOBAL_EVIDENCE_STORE
    _GLOBAL_EVIDENCE_STORE = store


def get_queue() -> Any:
    global _GLOBAL_QUEUE
    if _GLOBAL_QUEUE is None:
        from agents.queue import InMemoryAgentRunQueue

        _GLOBAL_QUEUE = InMemoryAgentRunQueue()
    return _GLOBAL_QUEUE


def set_queue(queue: Any) -> None:
    global _GLOBAL_QUEUE
    _GLOBAL_QUEUE = queue


def reset_all_stores() -> None:
    """Reset all global stores to fresh instances for test isolation."""
    global _GLOBAL_RUN_STORE, _GLOBAL_STEP_STORE
    global _GLOBAL_CHECKPOINT_STORE, _GLOBAL_EVIDENCE_STORE, _GLOBAL_QUEUE
    _GLOBAL_RUN_STORE = InMemoryRunStore()
    _GLOBAL_STEP_STORE = InMemoryStepStore()
    _GLOBAL_CHECKPOINT_STORE = DurableCheckpointStore(_GLOBAL_STEP_STORE)
    _GLOBAL_EVIDENCE_STORE = InMemoryEvidenceStore()
    from agents.queue import InMemoryAgentRunQueue

    _GLOBAL_QUEUE = InMemoryAgentRunQueue()
