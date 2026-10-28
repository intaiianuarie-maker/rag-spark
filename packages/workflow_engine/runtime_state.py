"""T-K07c: Workflow runtime state seam and storage protocol.

Decouples workflow run state, interactive approvals, and cancellation signaling
from module-level process globals into an injectable storage interface.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from workflow_engine.interfaces import WorkflowRun, WorkflowRunStatus


@runtime_checkable
class WorkflowRuntimeStateStore(Protocol):
    """Protocol for managing live workflow execution state across worker processes."""

    async def register_run(self, run: WorkflowRun) -> None:
        """Register a newly initialized workflow run."""
        ...

    async def get_run(self, workspace_id: str, run_id: str) -> WorkflowRun | None:
        """Lookup run state by workspace and run ID."""
        ...

    async def update_run_status(
        self,
        workspace_id: str,
        run_id: str,
        status: WorkflowRunStatus,
        error: str | None = None,
        final_output: Any = None,
    ) -> WorkflowRun:
        """Atomically transition run to a new lifecycle status."""
        ...

    async def register_approval(
        self,
        workspace_id: str,
        run_id: str,
        node_id: str,
        response: Any,
    ) -> None:
        """Record human approval outcome for a paused gate."""
        ...

    async def get_approval(
        self,
        workspace_id: str,
        run_id: str,
        node_id: str,
    ) -> Any | None:
        """Retrieve approval verdict for an interactive node."""
        ...

    async def request_cancellation(self, workspace_id: str, run_id: str) -> bool:
        """Signal cancellation across worker boundaries."""
        ...

    async def is_cancelled(self, run_id: str) -> bool:
        """Check whether cancellation was requested for run."""
        ...


class InMemoryWorkflowRuntimeStore:
    """Thread-safe and async-safe in-memory runtime store for testing and single-node dev."""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], WorkflowRun] = {}
        self._approvals: dict[tuple[str, str, str], Any] = {}
        self._cancellations: set[str] = set()
        self._lock = asyncio.Lock()

    async def register_run(self, run: WorkflowRun) -> None:
        async with self._lock:
            key = (run.workspace_id, run.id)
            self._runs[key] = run

    async def get_run(self, workspace_id: str, run_id: str) -> WorkflowRun | None:
        async with self._lock:
            return self._runs.get((workspace_id, run_id))

    async def update_run_status(
        self,
        workspace_id: str,
        run_id: str,
        status: WorkflowRunStatus,
        error: str | None = None,
        final_output: Any = None,
    ) -> WorkflowRun:
        async with self._lock:
            key = (workspace_id, run_id)
            run = self._runs.get(key)
            if run is None:
                raise ValueError(f"Workflow run not found: {workspace_id}/{run_id}")

            updates: dict[str, Any] = {"status": status}
            if error is not None:
                updates["error"] = error
            if final_output is not None:
                updates["final_output"] = final_output
            if status in (
                WorkflowRunStatus.COMPLETED,
                WorkflowRunStatus.FAILED,
                WorkflowRunStatus.CANCELLED,
            ):
                updates["finished_at"] = datetime.now(UTC).isoformat()

            updated_run = run.model_copy(update=updates)
            self._runs[key] = updated_run
            return updated_run

    async def register_approval(
        self,
        workspace_id: str,
        run_id: str,
        node_id: str,
        response: Any,
    ) -> None:
        async with self._lock:
            self._approvals[(workspace_id, run_id, node_id)] = response

    async def get_approval(
        self,
        workspace_id: str,
        run_id: str,
        node_id: str,
    ) -> Any | None:
        async with self._lock:
            return self._approvals.get((workspace_id, run_id, node_id))

    async def request_cancellation(self, workspace_id: str, run_id: str) -> bool:
        async with self._lock:
            key = (workspace_id, run_id)
            if key not in self._runs:
                return False
            self._cancellations.add(run_id)
            return True

    async def is_cancelled(self, run_id: str) -> bool:
        async with self._lock:
            return run_id in self._cancellations
