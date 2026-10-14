"""State management and store seams for V4 Visual Workflows (T-R19).

Integrates with DraftStore interface from packages/workflow_engine/draft_store.py
without modifying or coupling to packages/stores/* (dev1's R18 lane).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from workflow_engine.draft_store import DraftStore, InMemoryDraftStore
from workflow_engine.executor import WorkflowExecutor
from workflow_engine.interfaces import WorkflowRun, WorkflowRunStatus

from app.workflows.schemas import WorkflowApproveResponse

_lock = threading.Lock()
_draft_store: DraftStore | None = None
_approvals: dict[tuple[str, str, int], WorkflowApproveResponse] = {}
_runs: dict[tuple[str, str], WorkflowRun] = {}
_executors: dict[str, WorkflowExecutor] = {}


def get_draft_store() -> DraftStore:
    """Return the active DraftStore instance, defaulting to InMemoryDraftStore."""
    global _draft_store
    with _lock:
        if _draft_store is None:
            _draft_store = InMemoryDraftStore()
        return _draft_store


def set_draft_store(store: DraftStore) -> None:
    """Inject a custom or durable DraftStore."""
    global _draft_store
    with _lock:
        _draft_store = store


def record_approval(approval: WorkflowApproveResponse) -> None:
    """Record an approved workflow revision."""
    with _lock:
        key = (approval.workspace_id, approval.workflow_id, approval.revision)
        _approvals[key] = approval


def get_approval(
    workspace_id: str, workflow_id: str, revision: int
) -> WorkflowApproveResponse | None:
    """Look up an approval record for a specific revision."""
    with _lock:
        return _approvals.get((workspace_id, workflow_id, revision))


def save_run(run: WorkflowRun, executor: WorkflowExecutor | None = None) -> None:
    """Save a workflow run record and its active executor."""
    with _lock:
        _runs[(run.workspace_id, run.id)] = run
        if executor is not None:
            _executors[run.id] = executor


def get_run(workspace_id: str, run_id: str) -> WorkflowRun | None:
    """Retrieve a workflow run in a given workspace."""
    with _lock:
        return _runs.get((workspace_id, run_id))


def get_executor(run_id: str) -> WorkflowExecutor | None:
    """Retrieve the executor for an active run."""
    with _lock:
        return _executors.get(run_id)


def cancel_run(workspace_id: str, run_id: str) -> WorkflowRun | None:
    """Monotonically cancel a workflow run if not already terminal."""
    with _lock:
        run = _runs.get((workspace_id, run_id))
        if run is None:
            return None

        # Request cancellation on the active executor if running
        executor = _executors.get(run_id)
        if executor is not None:
            executor.request_cancel()

        # If already in terminal state, do not overwrite terminal completed/failed
        if run.status in (
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        ):
            return run

        # Transition status to CANCELLED monotonically
        updated_run = WorkflowRun(
            id=run.id,
            workspace_id=run.workspace_id,
            workflow_id=run.workflow_id,
            status=WorkflowRunStatus.CANCELLED,
            node_runs=run.node_runs,
            initial_input=run.initial_input,
            final_output=run.final_output,
            created_at=run.created_at,
            finished_at=datetime.now(UTC).isoformat(),
            error=run.error or "Run cancelled by user request",
            failed_node_id=run.failed_node_id,
        )
        _runs[(workspace_id, run_id)] = updated_run
        return updated_run


def reset_workflow_stores() -> None:
    """Reset all workflow stores for clean test isolation."""
    global _draft_store
    with _lock:
        _draft_store = InMemoryDraftStore()
        _approvals.clear()
        _runs.clear()
        _executors.clear()
