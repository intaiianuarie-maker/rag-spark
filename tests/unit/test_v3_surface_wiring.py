"""Unit tests for V3 surface route wiring, readiness, and real data tracing (T-R17).

Verifies the 3 target metrics:
1. surface fields rendering mocked/placeholder data as live: 0
   - Prove: output, usage, budget, and sources are drawn strictly from backend stores
     (DurableRunRecord.final_output / _PERSISTED_RUNS, StepStore, EvidenceStore).
2. cancel/approval actions bypassing backend transitions: 0
   - Prove: approval and cancel endpoints enforce monotonic server transitions.
3. rollout states misrepresenting readiness: 0
   - Prove: GET /readiness returns honest unavailable when LLM unconfigured, ready when configured.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from agents.interfaces import (
    ModelPolicy,
    RunBudget,
    RunContext,
    RunState,
    RunStatus,
    StepEvent,
    StepStatus,
    Usage,
)
from agents.policy import WorkspaceAgentPolicy, set_workspace_policy
from app.authz.principal import get_principal
from app.main import create_app
from core.auth import Principal, Role
from fastapi.testclient import TestClient
from stores import (
    Citation,
    DurableRunRecord,
    EvidenceRecord,
    get_evidence_store,
    get_queue,
    get_run_store,
    get_step_store,
    reset_all_stores,
)

pytestmark = pytest.mark.unit

WS_ID = str(uuid.uuid4())
USER_ID = "user-v3-surface"


@pytest.fixture(autouse=True)
def _setup_stores():
    reset_all_stores()
    set_workspace_policy(
        WS_ID,
        WorkspaceAgentPolicy(
            workspace_id=WS_ID,
            max_steps=10,
            max_tokens=50000,
            max_tool_calls=20,
            max_cost_usd=1.0,
            allowed_tools=("calculator", "knowledge_search", "knowledge_read"),
            allowed_models=("gpt-4o-mini", "gemini-flash"),
            default_model="gpt-4o-mini",
        ),
    )
    yield
    reset_all_stores()


def _principal():
    return Principal(
        user_id=USER_ID,
        email="test@example.com",
        is_authenticated=True,
        workspace_roles={WS_ID: Role.EDITOR},
    )


def _client():
    app = create_app()
    app.dependency_overrides[get_principal] = _principal
    return TestClient(app)


def test_readiness_unconfigured_honest_unavailable(monkeypatch):
    """When LLM provider is unconfigured, GET /readiness returns honest unavailable."""
    monkeypatch.delenv("LITELLM_URL", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    client = _client()
    resp = client.get(f"/v1/workspaces/{WS_ID}/agent/readiness")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ready"] is False
    assert data["status"] == "unavailable"
    assert data["missing"] == "llm_provider"
    assert "No LLM provider is configured" in data["detail"]


def test_readiness_configured_ready(monkeypatch):
    """When LLM provider credentials exist, GET /readiness reports ready."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key-12345")

    client = _client()
    resp = client.get(f"/v1/workspaces/{WS_ID}/agent/readiness")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ready"] is True
    assert data["status"] == "ready"
    assert data["missing"] is None


def test_status_response_includes_real_output_budget_usage_sources():
    """Verify StatusResponse aggregates real output, budget, usage, and sources."""
    run_id = str(uuid.uuid4())
    now_iso = datetime.now(UTC).isoformat()

    budget = RunBudget(
        max_steps=8,
        max_tokens=40000,
        max_tool_calls=15,
        max_cost_usd=0.75,
    )
    ctx = RunContext(
        run_id=run_id,
        workspace_id=WS_ID,
        principal_id=USER_ID,
        authz_policy_revision="rev1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=budget,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        allowed_tools=["calculator"],
    )
    record = DurableRunRecord(
        run_id=run_id,
        workspace_id=WS_ID,
        principal_id=USER_ID,
        goal="Calculate and verify revenue numbers",
        status=RunStatus.COMPLETED,
        context=ctx,
        state=RunState(status=RunStatus.COMPLETED),
        created_at=now_iso,
        updated_at=now_iso,
        final_output="Total revenue calculated is $1,250,000 across 3 quarters.",
    )
    get_run_store().create_run(record)

    # Seed real StepStore event with usage
    step_event = StepEvent(
        sequence=1,
        step_id="step-1",
        attempt_id="att-1",
        status=StepStatus.SUCCEEDED,
        action_summary="tool:calculator Used calculator for revenue sum",
        tool_name="calculator",
        timestamp=now_iso,
        latency_ms=120,
        usage=Usage(prompt_tokens=850, completion_tokens=150, tool_calls=1, cost_usd=0.0018),
    )
    get_step_store().append(run_id, step_event)

    # Seed real EvidenceStore citations
    ev_record = EvidenceRecord(
        id="ev-101",
        run_id=run_id,
        trace_id="tr-101",
        statement="Q3 revenue grew by 15%",
        verdict="supported",
        citations=[
            Citation(
                document_id="doc-abc-123",
                chunk_id="chunk-xyz-789",
                quote="Q3 gross revenue increased by 15% year-over-year.",
            )
        ],
    )
    ev_store = get_evidence_store()
    if hasattr(ev_store, "_store"):
        ev_store._store.setdefault(run_id, {})[ev_record.id] = ev_record

    client = _client()

    # Test GET /runs/{run_id}/status
    resp = client.get(f"/v1/workspaces/{WS_ID}/agent/runs/{run_id}/status")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["run_id"] == run_id
    assert data["status"] == "completed"
    assert data["goal"] == "Calculate and verify revenue numbers"
    assert data["output"] == "Total revenue calculated is $1,250,000 across 3 quarters."
    assert data["budget"]["max_steps"] == 8
    assert data["budget"]["max_cost_usd"] == 0.75
    assert data["usage"]["prompt_tokens"] == 850
    assert data["usage"]["completion_tokens"] == 150
    assert data["usage"]["cost_usd"] == pytest.approx(0.0018)
    assert len(data["sources"]) == 1
    assert data["sources"][0]["document_id"] == "doc-abc-123"
    assert data["sources"][0]["chunk_id"] == "chunk-xyz-789"
    assert data["sources"][0]["statement"] == "Q3 revenue grew by 15%"

    # Test GET /runs/{run_id} (router fallback)
    resp_fallback = client.get(f"/v1/workspaces/{WS_ID}/agent/runs/{run_id}")
    assert resp_fallback.status_code == 200, resp_fallback.text
    assert resp_fallback.json()["output"] == data["output"]
    assert resp_fallback.json()["usage"] == data["usage"]

    # Test GET /runs (workspace list)
    resp_list = client.get(f"/v1/workspaces/{WS_ID}/agent/runs")
    assert resp_list.status_code == 200, resp_list.text
    runs = resp_list.json()
    assert len(runs) >= 1
    found = next((r for r in runs if r["run_id"] == run_id), None)
    assert found is not None
    assert found["output"] == data["output"]


def test_approval_and_cancel_transitions():
    """Verify approval resume and cancel transitions on durable runs."""
    run_id = str(uuid.uuid4())
    now_iso = datetime.now(UTC).isoformat()

    ctx = RunContext(
        run_id=run_id,
        workspace_id=WS_ID,
        principal_id=USER_ID,
        authz_policy_revision="rev1",
        model_policy=ModelPolicy(allowed_models=["gpt-4o-mini"], default_model="gpt-4o-mini"),
        budget=RunBudget(max_steps=5, max_tokens=10000, max_tool_calls=5),
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        allowed_tools=["calculator"],
    )
    record = DurableRunRecord(
        run_id=run_id,
        workspace_id=WS_ID,
        principal_id=USER_ID,
        goal="Run needing approval",
        status=RunStatus.WAITING_APPROVAL,
        context=ctx,
        state=RunState(status=RunStatus.WAITING_APPROVAL),
        created_at=now_iso,
        updated_at=now_iso,
    )
    get_run_store().create_run(record)

    client = _client()

    # 1. Approval -> transitions to QUEUED and re-enqueues
    resp_app = client.post(
        f"/v1/workspaces/{WS_ID}/agent/runs/{run_id}/approval",
        json={"decision": "approved"},
    )
    assert resp_app.status_code == 200, resp_app.text
    assert resp_app.json()["status"] == "queued"
    assert resp_app.json()["decision"] == "approved"

    # Queue should now contain the run_id
    queue = get_queue()
    assert run_id in queue._ready

    # 2. Cancel -> transitions to CANCELLED
    resp_cancel = client.post(f"/v1/workspaces/{WS_ID}/agent/runs/{run_id}/cancel")
    assert resp_cancel.status_code == 200, resp_cancel.text
    assert resp_cancel.json()["status"] == "cancelled"

    # 3. Monotonic repeat cancel is idempotent no-op
    resp_cancel2 = client.post(f"/v1/workspaces/{WS_ID}/agent/runs/{run_id}/cancel")
    assert resp_cancel2.status_code == 200
    assert resp_cancel2.json()["status"] == "cancelled"
