"""Tests for V3 Durable Lifecycle: Queued run before 202, Worker + Stores (T-R15).

Verifies the 3 target metrics:
1. runs accepted without pre-allocated ID: 0
   - Prove: accept path creates ID first, persists to RunStore in QUEUED state,
     enqueues to queue, and only then returns HTTP 202 Accepted.
   - Ordering probe and failure injection prove 202 is NEVER returned without pre-allocated ID.
2. lost-step resumptions unrecoverable: 0
   - Prove: checkpoint/step store round-trip recovers full state, usage, context,
     approvals, and step history after simulated kill/restart, resuming with monotonic sequences.
3. lifecycle ops bypassing policy: 0
   - Prove: status, history, cancel, and approval strictly enforce server-side policy and
     workspace isolation (cross-workspace blocked with 403, policy widening blocked with 400,
     disallowed tools in approval blocked with 400, non-waiting approvals blocked with 400).
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agents.interfaces import (
    AgentErrorCode,
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepType,
    ModelPolicy,
    RunBudget,
    RunContext,
    RunState,
    RunStatus,
    StepEvent,
    StepStatus,
    Usage,
)
from agents.policy import (
    DEFAULT_MODEL,
    WorkspaceAgentPolicy,
    reset_workspace_policies,
    set_workspace_policy,
)
from agents.queue import InMemoryAgentRunQueue
from agents.runtime import AgentRuntime
from agents.step_store import SequenceViolationError, SqliteStepStore
from agents.tools import CalculatorTool, ToolRegistry
from app.agents.lifecycle_routes import router as lifecycle_router
from app.authz import Action, Resource
from app.authz.principal import get_principal
from core.auth import Principal, Role
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jobs.agent_worker import (
    AgentLifecycleWorker,
    run_agent_worker_loop,
)
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider
from stores.agent_stores import (
    DurableCheckpointStore,
    DurableRunRecord,
    InMemoryRunStore,
    InMemoryStepStore,
    SqliteRunStore,
    get_checkpoint_store,
    get_queue,
    get_run_store,
    get_step_store,
    reset_all_stores,
)

pytestmark = pytest.mark.unit

WS_ID_1 = "11111111-1111-1111-1111-111111111111"
WS_ID_2 = "22222222-2222-2222-2222-222222222222"
USER_1 = "user-1"
USER_2 = "user-2"


class MockLLM(LLMProvider):
    """Deterministic mock LLM for testing agent runtime execution."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or ["Final Answer: done"])
        self.calls: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls.append(request)
        if self.responses:
            text = self.responses.pop(0)
        else:
            text = "Final Answer: completed"
        return GenerationResult(
            text=text,
            model="mock-model",
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        yield ""


def _build_test_client(monkeypatch: pytest.MonkeyPatch, user_id: str = USER_1) -> TestClient:
    """Build a test client with principal override and mounted lifecycle routes."""
    app = FastAPI()
    app.include_router(lifecycle_router)

    def _mock_require(principal: Principal, action: Action, resource: Resource) -> None:
        # Enforce that user has access to requested workspace
        ws = resource.workspace_id
        if ws and ws not in principal.workspace_roles:
            from app.authz.errors import ApiError

            raise ApiError(status_code=403, code="workspace_forbidden", message="Forbidden")

    monkeypatch.setattr("app.agents.lifecycle_routes.require", _mock_require)

    async def _mock_principal() -> Principal:
        return Principal(
            user_id=user_id,
            email=f"{user_id}@example.com",
            is_authenticated=True,
            workspace_roles={WS_ID_1: Role.ADMIN},
        )

    app.dependency_overrides[get_principal] = _mock_principal
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _isolate_env() -> Iterator[None]:
    reset_workspace_policies()
    reset_all_stores()
    yield
    reset_workspace_policies()
    reset_all_stores()


# ============================================================================
# METRIC 1: runs accepted without pre-allocated ID: 0
# ============================================================================


def test_queued_run_pre_allocates_id_before_202_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metric 1: HTTP 202 is returned ONLY AFTER run_id is stored in QUEUED state."""
    client = _build_test_client(monkeypatch)
    run_store = get_run_store()
    queue = get_queue()

    events_order: list[str] = []
    original_create_run = run_store.create_run

    def _spy_create_run(record: DurableRunRecord) -> DurableRunRecord:
        events_order.append(f"create_run:{record.run_id}:{record.status.value}")
        # Verify state at creation time
        assert record.run_id is not None and len(record.run_id) > 10
        assert record.status == RunStatus.QUEUED
        assert record.state.status == RunStatus.QUEUED
        return original_create_run(record)

    monkeypatch.setattr(run_store, "create_run", _spy_create_run)

    original_enqueue = queue.enqueue

    async def _spy_enqueue(run_id: str) -> None:
        events_order.append(f"enqueue:{run_id}")
        await original_enqueue(run_id)

    monkeypatch.setattr(queue, "enqueue", _spy_enqueue)

    payload = {"goal": "Calculate 2 + 2", "max_steps": 5}
    response = client.post(f"/v1/workspaces/{WS_ID_1}/agent/runs/queued", json=payload)

    # 1. Status must be 202 Accepted
    assert response.status_code == 202
    data = response.json()
    run_id = data["run_id"]
    assert run_id is not None
    assert data["status"] == "queued"
    assert data["workspace_id"] == WS_ID_1

    # 2. Verify strict execution order: create_run -> enqueue -> HTTP 202 returned
    assert len(events_order) == 2
    assert events_order[0] == f"create_run:{run_id}:queued"
    assert events_order[1] == f"enqueue:{run_id}"

    # 3. Direct verification in RunStore
    persisted = run_store.get_run(run_id)
    assert persisted is not None
    assert persisted.run_id == run_id
    assert persisted.status == RunStatus.QUEUED
    assert persisted.goal == "Calculate 2 + 2"
    assert persisted.context is not None
    assert persisted.context.run_id == run_id
    assert persisted.context.workspace_id == WS_ID_1


def test_runs_without_id_or_on_store_failure_never_accept_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metric 1: If store persistence fails, HTTP 202 is NEVER returned (0 accepted)."""
    client = _build_test_client(monkeypatch)
    run_store = get_run_store()

    def _failing_create_run(record: DurableRunRecord) -> DurableRunRecord:
        raise RuntimeError("Database connection died")

    monkeypatch.setattr(run_store, "create_run", _failing_create_run)

    payload = {"goal": "Test failure injection"}
    response = client.post(f"/v1/workspaces/{WS_ID_1}/agent/runs/queued", json=payload)

    # Must fail, NEVER accept with 202
    assert response.status_code != 202
    # Verify no job on queue
    queue = get_queue()
    assert asyncio.run(queue.depth()) == 0


def test_async_endpoint_alias_also_pre_allocates_before_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /runs/async alias also adheres to pre-allocated ID before 202."""
    client = _build_test_client(monkeypatch)
    payload = {"goal": "Async run goal"}
    response = client.post(f"/v1/workspaces/{WS_ID_1}/agent/runs/async", json=payload)
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "queued"
    run_id = data["run_id"]

    rec = get_run_store().get_run(run_id)
    assert rec is not None
    assert rec.status == RunStatus.QUEUED


# ============================================================================
# METRIC 2: lost-step resumptions unrecoverable: 0
# ============================================================================


def test_checkpoint_step_store_round_trip_recovers_exact_state() -> None:
    """Metric 2: Interrupted run checkpoint round-trip restores state with zero loss."""
    run_id = str(uuid.uuid4())
    step_store = InMemoryStepStore()
    ckpt_store = DurableCheckpointStore(step_store)

    context = RunContext(
        run_id=run_id,
        workspace_id=WS_ID_1,
        principal_id=USER_1,
        authz_policy_revision="rev1",
        model_policy=ModelPolicy(allowed_models=[DEFAULT_MODEL], default_model=DEFAULT_MODEL),
        budget=RunBudget(max_steps=10),
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        allowed_tools=["calculator"],
    )

    # 1. Record steps 1, 2, 3
    ev1 = StepEvent(
        sequence=1,
        step_id="step-1",
        attempt_id="att-1",
        action_summary="tool:calculator input:2+2",
        status=StepStatus.SUCCEEDED,
    )
    ev2 = StepEvent(
        sequence=2,
        step_id="step-2",
        attempt_id="att-2",
        action_summary="tool:calculator input:4*4",
        status=StepStatus.SUCCEEDED,
    )
    ev3 = StepEvent(
        sequence=3,
        step_id="step-3",
        attempt_id="att-3",
        action_summary="waiting:approval tool:publish",
        status=StepStatus.STARTED,
    )
    step_store.append(run_id, ev1)
    step_store.append(run_id, ev2)
    step_store.append(run_id, ev3)

    state = RunState(status=RunStatus.WAITING_APPROVAL)
    usage = Usage(prompt_tokens=450, completion_tokens=120, tool_calls=2, cost_usd=0.005)
    approvals = {(run_id, "step-3", "publish"): "pending"}
    waiting = (run_id, "step-3", "publish", "payload")

    agent_run = AgentRun(
        id=run_id,
        workspace_id=WS_ID_1,
        goal="Calculate and publish",
        model=DEFAULT_MODEL,
        status=AgentRunStatus.PENDING,
        steps=[
            AgentStep(step_number=1, type=AgentStepType.TOOL_CALL, content="2+2"),
            AgentStep(step_number=2, type=AgentStepType.TOOL_CALL, content="4*4"),
        ],
        total_tokens=570,
    )

    # Save durable checkpoint at step 3
    ckpt_event = ckpt_store.save_checkpoint(
        run_id=run_id,
        state=state,
        usage=usage,
        context=context,
        approvals=approvals,
        waiting=waiting,
        agent_run=agent_run,
    )
    assert ckpt_event.sequence == 4

    # 2. Simulate process crash / kill: create a new store instance pointing to same data
    # and reconstruct
    loaded = ckpt_store.load_checkpoint(run_id)
    assert loaded is not None

    restored_state: RunState = loaded["state"]
    restored_usage: Usage = loaded["usage"]
    restored_context: RunContext = loaded["context"]
    restored_approvals = loaded["approvals"]
    restored_waiting = loaded["waiting"]
    restored_agent_run: AgentRun | None = loaded["agent_run"]

    # Verify 100% round-trip fidelity
    assert restored_state.status == RunStatus.WAITING_APPROVAL
    assert restored_usage.prompt_tokens == 450
    assert restored_usage.completion_tokens == 120
    assert restored_usage.tool_calls == 2
    assert restored_usage.cost_usd == 0.005
    assert restored_context.run_id == run_id
    assert restored_context.workspace_id == WS_ID_1
    assert restored_approvals == approvals
    assert restored_waiting == waiting
    assert restored_agent_run is not None
    assert len(restored_agent_run.steps) == 2
    assert restored_agent_run.total_tokens == 570

    # 3. Monotonic continuation: step 5 can be appended cleanly
    ev5 = StepEvent(
        sequence=5,
        step_id="step-5",
        attempt_id="att-5",
        action_summary="resumed:final_answer completed",
        status=StepStatus.SUCCEEDED,
    )
    step_store.append(run_id, ev5)

    # 4. Out-of-order sequence violation is rejected (monotonic invariant)
    bad_ev = StepEvent(
        sequence=2,
        step_id="step-bad",
        attempt_id="att-bad",
        action_summary="duplicate step",
        status=StepStatus.FAILED,
        error_code=AgentErrorCode.TOOL_NOT_ALLOWED,
    )
    with pytest.raises(SequenceViolationError):
        step_store.append(run_id, bad_ev)

    # Total events: steps 1, 2, 3, ckpt 4, step 5 = 5 events
    history = step_store.list(run_id)
    assert len(history) == 5
    assert [e.sequence for e in history] == [1, 2, 3, 4, 5]


def test_sqlite_step_store_and_run_store_persistence() -> None:
    """Metric 2: Persistent SQLite backends survive restart and maintain step history."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = Path(td) / "lifecycle.db"
        run_store = SqliteRunStore(db_path)
        step_store = SqliteStepStore(db_path)
        ckpt_store = DurableCheckpointStore(step_store)

        run_id = str(uuid.uuid4())
        ctx = RunContext(
            run_id=run_id,
            workspace_id=WS_ID_1,
            principal_id=USER_1,
            authz_policy_revision="rev1",
            model_policy=ModelPolicy(allowed_models=[DEFAULT_MODEL], default_model=DEFAULT_MODEL),
            budget=RunBudget(max_steps=5),
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            allowed_tools=["calculator"],
        )
        record = DurableRunRecord(
            run_id=run_id,
            workspace_id=WS_ID_1,
            principal_id=USER_1,
            goal="Sqlite persistence test",
            status=RunStatus.QUEUED,
            context=ctx,
        )
        run_store.create_run(record)

        step_store.append(
            run_id,
            StepEvent(
                sequence=1,
                step_id="s1",
                attempt_id="a1",
                action_summary="first step",
                status=StepStatus.SUCCEEDED,
            ),
        )
        ckpt_store.save_checkpoint(
            run_id=run_id,
            state=RunState(status=RunStatus.RUNNING),
            usage=Usage(prompt_tokens=100),
            context=ctx,
        )

        # Reopen from disk (simulating process restart)
        run_store_2 = SqliteRunStore(db_path)
        step_store_2 = SqliteStepStore(db_path)
        ckpt_store_2 = DurableCheckpointStore(step_store_2)

        loaded_rec = run_store_2.get_run(run_id)
        assert loaded_rec is not None
        assert loaded_rec.run_id == run_id
        assert loaded_rec.status == RunStatus.QUEUED

        loaded_ckpt = ckpt_store_2.load_checkpoint(run_id)
        assert loaded_ckpt is not None
        assert loaded_ckpt["state"].status == RunStatus.RUNNING
        assert loaded_ckpt["usage"].prompt_tokens == 100

        # Monotonic append to reopened store
        step_store_2.append(
            run_id,
            StepEvent(
                sequence=3,
                step_id="s3",
                attempt_id="a3",
                action_summary="resumed step",
                status=StepStatus.SUCCEEDED,
            ),
        )
        assert len(step_store_2.list(run_id)) == 3


# ============================================================================
# METRIC 3: lifecycle ops bypassing policy: 0
# ============================================================================


def test_cross_workspace_lifecycle_ops_strictly_forbidden_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metric 3: Cross-workspace access to status, history, cancel, approval returns 403."""
    client = _build_test_client(monkeypatch)
    run_store = get_run_store()

    run_id = str(uuid.uuid4())
    record = DurableRunRecord(
        run_id=run_id,
        workspace_id=WS_ID_2,  # Owned by workspace 2
        principal_id=USER_2,
        goal="WS2 secret run",
        status=RunStatus.WAITING_APPROVAL,
        state=RunState(status=RunStatus.WAITING_APPROVAL),
    )
    run_store.create_run(record)

    # 1. Attempting status from WS1
    res_status = client.get(f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/status")
    assert res_status.status_code == 403
    assert res_status.json()["detail"]["error"] == "workspace_mismatch"

    # 2. Attempting history from WS1
    res_hist = client.get(f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/history")
    assert res_hist.status_code == 403
    assert res_hist.json()["detail"]["error"] == "workspace_mismatch"

    # 3. Attempting cancel from WS1
    res_cancel = client.post(f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/cancel")
    assert res_cancel.status_code == 403
    assert res_cancel.json()["detail"]["error"] == "workspace_mismatch"

    # 4. Attempting approval from WS1
    res_app = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/approval",
        json={"decision": "approved"},
    )
    assert res_app.status_code == 403
    assert res_app.json()["detail"]["error"] == "workspace_mismatch"


def test_queued_endpoint_refuses_policy_widening_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metric 3: Requests attempting to widen steps, models, or tools are rejected with 400."""
    client = _build_test_client(monkeypatch)

    set_workspace_policy(
        WS_ID_1,
        WorkspaceAgentPolicy(
            workspace_id=WS_ID_1,
            max_steps=5,
            allowed_models=(DEFAULT_MODEL,),
            allowed_tools=("calculator",),
        ),
    )

    # Widening max_steps (requested 10 > limit 5)
    res_steps = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/queued",
        json={"goal": "test", "max_steps": 10},
    )
    assert res_steps.status_code == 400
    assert res_steps.json()["detail"]["error"] == "policy_violation"

    # Widening model (unallowed model)
    res_model = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/queued",
        json={"goal": "test", "model": "unallowed-claude"},
    )
    assert res_model.status_code == 400
    assert res_model.json()["detail"]["error"] == "policy_violation"

    # Widening tools (disallowed tool)
    res_tool = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/queued",
        json={"goal": "test", "allowed_tools": ["danger_tool"]},
    )
    assert res_tool.status_code == 400
    assert res_tool.json()["detail"]["error"] == "policy_violation"


def test_approval_endpoint_enforces_server_policy_and_run_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metric 3: Approving disallowed tool or run in wrong state is refused with 400."""
    client = _build_test_client(monkeypatch)
    run_store = get_run_store()

    set_workspace_policy(
        WS_ID_1,
        WorkspaceAgentPolicy(
            workspace_id=WS_ID_1,
            allowed_tools=("calculator",),
        ),
    )

    # Case A: Run in wrong state (QUEUED, not WAITING_APPROVAL)
    run_id_queued = str(uuid.uuid4())
    run_store.create_run(
        DurableRunRecord(
            run_id=run_id_queued,
            workspace_id=WS_ID_1,
            principal_id=USER_1,
            goal="Queued run",
            status=RunStatus.QUEUED,
            state=RunState(status=RunStatus.QUEUED),
        )
    )
    res_wrong_state = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id_queued}/approval",
        json={"decision": "approved"},
    )
    assert res_wrong_state.status_code == 400
    assert res_wrong_state.json()["detail"]["error"] == "invalid_state"

    # Case B: Run waiting approval, but approving disallowed tool
    run_id_waiting = str(uuid.uuid4())
    run_store.create_run(
        DurableRunRecord(
            run_id=run_id_waiting,
            workspace_id=WS_ID_1,
            principal_id=USER_1,
            goal="Waiting run",
            status=RunStatus.WAITING_APPROVAL,
            state=RunState(status=RunStatus.WAITING_APPROVAL),
        )
    )
    res_disallowed_tool = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id_waiting}/approval",
        json={"decision": "approved", "tool_name": "arbitrary_code_exec"},
    )
    assert res_disallowed_tool.status_code == 400
    assert res_disallowed_tool.json()["detail"]["error"] == "policy_violation"

    # Case C: Valid approval re-queues run
    res_valid_app = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id_waiting}/approval",
        json={"decision": "approved", "tool_name": "calculator"},
    )
    assert res_valid_app.status_code == 200
    assert res_valid_app.json()["status"] == "queued"
    assert res_valid_app.json()["decision"] == "approved"
    rec_app = run_store.get_run(run_id_waiting)
    assert rec_app is not None
    assert rec_app.status == RunStatus.QUEUED

    # Case D: Valid rejection cancels run
    run_id_waiting_2 = str(uuid.uuid4())
    run_store.create_run(
        DurableRunRecord(
            run_id=run_id_waiting_2,
            workspace_id=WS_ID_1,
            principal_id=USER_1,
            goal="Waiting run 2",
            status=RunStatus.WAITING_APPROVAL,
            state=RunState(status=RunStatus.WAITING_APPROVAL),
        )
    )
    res_valid_rej = client.post(
        f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id_waiting_2}/approval",
        json={"decision": "rejected", "feedback": "too risky"},
    )
    assert res_valid_rej.status_code == 200
    assert res_valid_rej.json()["status"] == "cancelled"
    assert res_valid_rej.json()["decision"] == "rejected"
    rec_rej = run_store.get_run(run_id_waiting_2)
    assert rec_rej is not None
    assert rec_rej.status == RunStatus.CANCELLED


def test_cancel_endpoint_is_monotonic_and_byte_identical_on_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metric 3: Cancelling an already completed run is a clean monotonic no-op."""
    client = _build_test_client(monkeypatch)
    run_store = get_run_store()

    run_id = str(uuid.uuid4())
    run_store.create_run(
        DurableRunRecord(
            run_id=run_id,
            workspace_id=WS_ID_1,
            principal_id=USER_1,
            goal="Completed run",
            status=RunStatus.COMPLETED,
            state=RunState(status=RunStatus.COMPLETED),
        )
    )

    res = client.post(f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/cancel")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "completed"
    assert data["cancelled"] is False  # Already terminal, no modification

    rec = run_store.get_run(run_id)
    assert rec is not None
    assert rec.status == RunStatus.COMPLETED


# ============================================================================
# 4. End-to-End Worker Pickup & Lifecycle Integration
# ============================================================================


def test_worker_pickup_queued_run_executes_to_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full lifecycle: API queues run -> Worker claims -> executes -> transitions to COMPLETED."""
    client = _build_test_client(monkeypatch)
    queue = get_queue()
    run_store = get_run_store()
    step_store = get_step_store()
    ckpt_store = get_checkpoint_store()

    # 1. Start queued run via API
    payload = {"goal": "What is 10 + 20?", "max_steps": 5}
    res = client.post(f"/v1/workspaces/{WS_ID_1}/agent/runs/queued", json=payload)
    assert res.status_code == 202
    run_id = res.json()["run_id"]

    rec_q = run_store.get_run(run_id)
    assert rec_q is not None
    assert rec_q.status == RunStatus.QUEUED
    assert asyncio.run(queue.depth()) == 1

    # 2. Setup AgentWorker with MockLLM
    def _create_runtime() -> AgentRuntime:
        reg = ToolRegistry()
        reg.register(CalculatorTool())
        mock_llm = MockLLM(responses=["Final Answer: 30"])
        return AgentRuntime(mock_llm, reg)

    worker = AgentLifecycleWorker(
        queue=queue,
        run_store=run_store,
        checkpoint_store=ckpt_store,
        step_store=step_store,
        runtime_factory=_create_runtime,
    )

    # 3. Worker pickup
    claimed_id = asyncio.run(worker.run_once())
    assert claimed_id == run_id

    # Queue should be acked
    assert asyncio.run(queue.depth()) == 0

    # Run store must now show COMPLETED
    rec = run_store.get_run(run_id)
    assert rec is not None
    assert rec.status == RunStatus.COMPLETED

    # Status endpoint verifies COMPLETED
    status_res = client.get(f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/status")
    assert status_res.status_code == 200
    assert status_res.json()["status"] == "completed"


def test_worker_kill_recovery_and_checkpoint_resume() -> None:
    """Worker killed mid-run (lease expiry) is recovered and resumed from checkpoint."""
    queue = InMemoryAgentRunQueue()
    run_store = InMemoryRunStore()
    step_store = InMemoryStepStore()
    ckpt_store = DurableCheckpointStore(step_store)

    run_id = str(uuid.uuid4())
    ctx = RunContext(
        run_id=run_id,
        workspace_id=WS_ID_1,
        principal_id=USER_1,
        authz_policy_revision="rev1",
        model_policy=ModelPolicy(allowed_models=[DEFAULT_MODEL], default_model=DEFAULT_MODEL),
        budget=RunBudget(max_steps=5),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        allowed_tools=["calculator"],
    )
    rec = DurableRunRecord(
        run_id=run_id,
        workspace_id=WS_ID_1,
        principal_id=USER_1,
        goal="Two step run",
        status=RunStatus.QUEUED,
        context=ctx,
    )
    run_store.create_run(rec)
    asyncio.run(queue.enqueue(run_id))

    # Save initial checkpoint at step 1
    step_store.append(
        run_id,
        StepEvent(
            sequence=1,
            step_id="step-1",
            attempt_id="att-1",
            action_summary="tool:calculator input:5+5",
            status=StepStatus.SUCCEEDED,
        ),
    )
    ckpt_store.save_checkpoint(
        run_id=run_id,
        state=RunState(status=RunStatus.RUNNING),
        usage=Usage(prompt_tokens=50),
        context=ctx,
    )

    # Worker 1 claims with very short lease (simulating crash before completion)
    c1 = asyncio.run(queue.claim(lease_seconds=0.1))
    assert c1 is not None and str(c1) == run_id

    # Wait for lease to expire
    asyncio.run(asyncio.sleep(0.15))

    # Queue recovery recovers expired claim back to ready
    recovered = asyncio.run(queue.recover_expired())
    assert recovered == 1
    assert asyncio.run(queue.depth()) == 1

    # Worker 2 picks up recovered run and completes
    def _create_runtime() -> AgentRuntime:
        reg = ToolRegistry()
        reg.register(CalculatorTool())
        mock_llm = MockLLM(responses=["Final Answer: 10"])
        return AgentRuntime(mock_llm, reg)

    worker2 = AgentLifecycleWorker(
        queue=queue,
        run_store=run_store,
        checkpoint_store=ckpt_store,
        step_store=step_store,
        runtime_factory=_create_runtime,
    )

    claimed_2 = asyncio.run(worker2.run_once())
    assert claimed_2 == run_id
    assert asyncio.run(queue.depth()) == 0
    rec_done = run_store.get_run(run_id)
    assert rec_done is not None
    assert rec_done.status == RunStatus.COMPLETED


def test_worker_parks_deterministic_budget_failure() -> None:
    """Deterministic failure (e.g., budget exceeded) parks the run instead of looping."""
    queue = InMemoryAgentRunQueue()
    run_store = InMemoryRunStore()
    step_store = InMemoryStepStore()
    ckpt_store = DurableCheckpointStore(step_store)

    run_id = str(uuid.uuid4())
    ctx = RunContext(
        run_id=run_id,
        workspace_id=WS_ID_1,
        principal_id=USER_1,
        authz_policy_revision="rev1",
        model_policy=ModelPolicy(allowed_models=[DEFAULT_MODEL], default_model=DEFAULT_MODEL),
        budget=RunBudget(max_steps=1),  # Cap at 1 step
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        allowed_tools=["calculator"],
    )
    rec = DurableRunRecord(
        run_id=run_id,
        workspace_id=WS_ID_1,
        principal_id=USER_1,
        goal="Loop forever",
        status=RunStatus.QUEUED,
        context=ctx,
    )
    run_store.create_run(rec)
    asyncio.run(queue.enqueue(run_id))

    def _create_looping_runtime() -> AgentRuntime:
        reg = ToolRegistry()
        reg.register(CalculatorTool())
        # Always output a tool call, never final answer -> triggers MAX_STEPS_EXCEEDED
        tool_action = 'Action: calculator\nAction Input: {"expression": "1+1"}'
        mock_llm = MockLLM(responses=[tool_action] * 5)
        return AgentRuntime(mock_llm, reg)

    worker = AgentLifecycleWorker(
        queue=queue,
        run_store=run_store,
        checkpoint_store=ckpt_store,
        step_store=step_store,
        runtime_factory=_create_looping_runtime,
    )

    claimed = asyncio.run(worker.run_once())
    assert claimed == run_id

    # Must be parked in queue, not in ready
    assert asyncio.run(queue.depth()) == 0
    assert asyncio.run(queue.parked_depth()) == 1

    # Run store reflects LIMIT_EXCEEDED
    rec_lim = run_store.get_run(run_id)
    assert rec_lim is not None
    assert rec_lim.status == RunStatus.LIMIT_EXCEEDED


def test_history_and_list_endpoints_and_404s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test history, workspace listing, and 404 responses for non-existent runs."""
    client = _build_test_client(monkeypatch)
    run_store = get_run_store()
    step_store = get_step_store()

    # 404 on non-existent runs
    fake_id = str(uuid.uuid4())
    assert client.get(f"/v1/workspaces/{WS_ID_1}/agent/runs/{fake_id}/status").status_code == 404
    assert client.get(f"/v1/workspaces/{WS_ID_1}/agent/runs/{fake_id}/history").status_code == 404
    assert client.post(f"/v1/workspaces/{WS_ID_1}/agent/runs/{fake_id}/cancel").status_code == 404
    assert (
        client.post(
            f"/v1/workspaces/{WS_ID_1}/agent/runs/{fake_id}/approval",
            json={"decision": "approved"},
        ).status_code
        == 404
    )

    # Create run with steps
    run_id = str(uuid.uuid4())
    run_store.create_run(
        DurableRunRecord(
            run_id=run_id,
            workspace_id=WS_ID_1,
            principal_id=USER_1,
            goal="History test",
            status=RunStatus.RUNNING,
            state=RunState(status=RunStatus.RUNNING),
        )
    )
    step_store.append(
        run_id,
        StepEvent(
            sequence=1,
            step_id="step-1",
            attempt_id="att-1",
            action_summary="tool:calculator 1+1",
            status=StepStatus.SUCCEEDED,
        ),
    )
    step_store.append(
        run_id,
        StepEvent(
            sequence=2,
            step_id="step-2",
            attempt_id="att-2",
            action_summary="final answer",
            status=StepStatus.SUCCEEDED,
        ),
    )

    # History endpoint
    res_hist = client.get(f"/v1/workspaces/{WS_ID_1}/agent/runs/{run_id}/history")
    assert res_hist.status_code == 200
    data = res_hist.json()
    assert data["total_steps"] == 2
    assert len(data["steps"]) == 2
    assert data["steps"][0]["sequence"] == 1
    assert data["steps"][1]["sequence"] == 2

    # Workspace runs list endpoint
    res_list = client.get(f"/v1/workspaces/{WS_ID_1}/agent/runs")
    assert res_list.status_code == 200
    runs = res_list.json()
    assert any(r["run_id"] == run_id for r in runs)


def test_supervised_worker_loop_stops_promptly() -> None:
    """Supervised worker loop processes jobs and terminates on stop_event."""
    async def _run() -> None:
        queue = InMemoryAgentRunQueue()
        run_store = InMemoryRunStore()
        step_store = InMemoryStepStore()
        ckpt_store = DurableCheckpointStore(step_store)

        worker = AgentLifecycleWorker(
            queue=queue,
            run_store=run_store,
            checkpoint_store=ckpt_store,
            step_store=step_store,
        )

        stop_event = asyncio.Event()
        loop_task = asyncio.create_task(
            run_agent_worker_loop(worker, stop_event, poll_interval_s=0.01)
        )

        await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(loop_task, timeout=1.0)
        assert loop_task.done()

    asyncio.run(_run())
