"""Unit tests for T-K07c: Workflow runtime state seam contract and in-memory implementation."""

from __future__ import annotations

import pytest
from workflow_engine.interfaces import WorkflowRun, WorkflowRunStatus
from workflow_engine.runtime_state import (
    InMemoryWorkflowRuntimeStore,
    WorkflowRuntimeStateStore,
)

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_runtime_state_store_protocol_conformance() -> None:
    store = InMemoryWorkflowRuntimeStore()
    assert isinstance(store, WorkflowRuntimeStateStore)


@pytest.mark.asyncio
async def test_runtime_state_lifecycle() -> None:
    store = InMemoryWorkflowRuntimeStore()
    run = WorkflowRun(
        id="run-100",
        workspace_id="ws-seam",
        workflow_id="wf-test",
        status=WorkflowRunStatus.PENDING,
    )

    # 1. Register & Retrieve
    await store.register_run(run)
    fetched = await store.get_run("ws-seam", "run-100")
    assert fetched is not None
    assert fetched.id == "run-100"
    assert fetched.status == WorkflowRunStatus.PENDING

    # 2. Update status
    updated = await store.update_run_status("ws-seam", "run-100", WorkflowRunStatus.RUNNING)
    assert updated.status == WorkflowRunStatus.RUNNING

    # 3. Interactive approval
    await store.register_approval("ws-seam", "run-100", "node-gate", {"approved": True})
    approval = await store.get_approval("ws-seam", "run-100", "node-gate")
    assert approval == {"approved": True}

    # 4. Cancellation signaling
    assert not await store.is_cancelled("run-100")
    cancelled = await store.request_cancellation("ws-seam", "run-100")
    assert cancelled is True
    assert await store.is_cancelled("run-100")

    # 5. Terminal update
    terminal = await store.update_run_status(
        "ws-seam",
        "run-100",
        WorkflowRunStatus.COMPLETED,
        final_output={"summary": "all done"},
    )
    assert terminal.status == WorkflowRunStatus.COMPLETED
    assert terminal.final_output == {"summary": "all done"}
    assert terminal.finished_at is not None
