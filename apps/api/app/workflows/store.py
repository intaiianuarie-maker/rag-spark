"""State management and store seams for V4 Visual Workflows (T-R19).

Integrates with DraftStore interface from packages/workflow_engine/draft_store.py
without modifying or coupling to packages/stores/* (dev1's R18 lane).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from workflow_engine.draft_store import DraftStore, InMemoryDraftStore
from workflow_engine.executor import WorkflowExecutor
from workflow_engine.interfaces import WorkflowRun, WorkflowRunStatus

from app.workflows.schemas import WorkflowApproveResponse

_RUNS_DB = None  # lazy


def runs_db_path() -> Path:
    raw = os.environ.get("WORKFLOW_RUNS_DB", "").strip()
    if raw is None or not raw.strip():
        raise ValueError("WORKFLOW_RUNS_DB is required")
    return Path(raw.strip())


def approvals_db_path() -> Path:
    raw = os.environ.get("WORKFLOW_APPROVALS_DB", "").strip()
    if raw is None or not raw.strip():
        raise ValueError("WORKFLOW_APPROVALS_DB is required")
    return Path(raw.strip())


def _init_runs_db(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE IF NOT EXISTS runs (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        con.commit()


def _init_approvals_db(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        stmt = "CREATE TABLE IF NOT EXISTS approvals (key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        con.execute(stmt)
        con.commit()


def _runs_connect():
    path = runs_db_path()
    _init_runs_db(path)
    return sqlite3.connect(path)


def _approvals_connect():
    path = approvals_db_path()
    _init_approvals_db(path)
    return sqlite3.connect(path)



def draft_store_mode() -> str:
    import os
    return "persistent" if os.environ.get("WORKFLOW_DRAFT_DB", "").strip() else "in-memory"


def runs_store_mode() -> str:
    import os
    return "persistent" if os.environ.get("WORKFLOW_RUNS_DB", "").strip() else "in-memory"


def approvals_store_mode() -> str:
    import os
    return "persistent" if os.environ.get("WORKFLOW_APPROVALS_DB", "").strip() else "in-memory"


def checkpoint_db_mode() -> str:
    import os
    return "persistent" if os.environ.get("WORKFLOW_CHECKPOINT_DB", "").strip() else "unset"

_lock = threading.Lock()
_draft_store: DraftStore | None = None
_approvals: dict[tuple[str, str, int], WorkflowApproveResponse] = {}
_runs: dict[tuple[str, str], WorkflowRun] = {}
_executors: dict[str, WorkflowExecutor] = {}


def draft_db_path() -> Path:
    import os

    raw = os.environ.get("WORKFLOW_DRAFT_DB", "").strip()
    if raw is None or not raw.strip():
        raise ValueError("WORKFLOW_DRAFT_DB is required")
    return Path(raw.strip())


def get_draft_store() -> DraftStore:
    """Return the active DraftStore instance (durable Sqlite by default)."""
    global _draft_store
    with _lock:
        if _draft_store is None:
            from stores.workflow_draft_store import SqliteDraftStore

            _draft_store = SqliteDraftStore(draft_db_path())
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
    try:
        with _approvals_connect() as con:
            db_key = f"{approval.workspace_id}:{approval.workflow_id}:{approval.revision}"
            payload = approval.model_dump_json()
            con.execute(
                "INSERT OR REPLACE INTO approvals (key, payload) VALUES (?, ?)",
                (db_key, payload),
            )
            con.commit()
    except ValueError:
        raise


def get_approval(
    workspace_id: str, workflow_id: str, revision: int
) -> WorkflowApproveResponse | None:
    """Look up an approval record for a specific revision."""
    with _lock:
        cached = _approvals.get((workspace_id, workflow_id, revision))
        if cached is not None:
            return cached
    try:
        with _approvals_connect() as con:
            key = f"{workspace_id}:{workflow_id}:{revision}"
            cur = con.execute("SELECT payload FROM approvals WHERE key=?", (key,))
            row = cur.fetchone()
            if row:
                from app.workflows.schemas import WorkflowApproveResponse

                return WorkflowApproveResponse.model_validate_json(row[0])
    except ValueError:
        raise
    return None


def save_run(run: WorkflowRun, executor: WorkflowExecutor | None = None) -> None:
    """Save a workflow run record and its active executor."""
    payload = run.model_dump_json()
    with _lock:
        _runs[(run.workspace_id, run.id)] = run
        if executor is not None:
            _executors[run.id] = executor
    with _runs_connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO runs (key, payload) VALUES (?, ?)",
            (f"{run.workspace_id}:{run.id}", payload),
        )
        con.commit()


def get_run(workspace_id: str, run_id: str) -> WorkflowRun | None:
    """Retrieve a workflow run in a given workspace."""
    with _lock:
        cached = _runs.get((workspace_id, run_id))
        if cached is not None:
            return cached
    try:
        with _runs_connect() as con:
            cur = con.execute("SELECT payload FROM runs WHERE key=?", (f"{workspace_id}:{run_id}",))
            row = cur.fetchone()
            if row:
                run = WorkflowRun.model_validate_json(row[0])
                with _lock:
                    _runs[(workspace_id, run_id)] = run
                return run
    except ValueError:
        raise
    return None


def get_executor(run_id: str) -> WorkflowExecutor | None:
    """Retrieve the executor for an active run."""
    with _lock:
        return _executors.get(run_id)


def cancel_run(workspace_id: str, run_id: str) -> WorkflowRun | None:
    """Monotonically cancel a workflow run if not already terminal."""
    run = get_run(workspace_id, run_id)
    if run is None:
        return None

    with _lock:
        # No live executor means no in-flight run to cancel (post-restart).
        executor = _executors.get(run_id)
        if executor is None and run.status not in (
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        ):
            raise ValueError("no live executor for run")

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
        try:
            with _runs_connect() as con:
                key = f"{workspace_id}:{run_id}"
                payload = updated_run.model_dump_json()
                con.execute(
                    "INSERT OR REPLACE INTO runs (key, payload) VALUES (?, ?)",
                    (key, payload),
                )
                con.commit()
        except ValueError:
            raise
        return updated_run


def reset_workflow_stores() -> None:
    """Reset all workflow stores for clean test isolation."""
    global _draft_store
    with _lock:
        _draft_store = InMemoryDraftStore()
        _approvals.clear()
        _runs.clear()
        _executors.clear()
