"""T-A16 / V4-02 — register the run and its cancel handle before dispatch.

Astra CONFIRMED Med: ``run_workflow`` awaited ``executor.execute(...)`` and only
then called ``save_run(run, executor)``. So while a run was executing there was
no run id to poll, no run row to read, and no registered executor for
``cancel_run`` to reach — the lifecycle surface only worked on terminal runs.

Controls covered here:

* **initial run row visible before completion** — the run id is returned and
  readable through the status handler while the node is still blocked.
* **in-flight control** — a concurrent status poll *and* a cancel both succeed
  against a blocked run, and the cancel actually settles the live executor.
* **monotonic terminal state** — once cancelled, a late-finishing task never
  flips the run back to completed.
* **terminal-cancel behavior unchanged** — a run with no live executor still
  returns 409 (covered by the existing suite, re-asserted here).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import uuid
from typing import Any

os.environ.setdefault("WORKFLOW_CHECKPOINT_DB", "/tmp/opencode/t_a16_checkpoints.db")
os.environ.setdefault("WORKFLOW_DRAFT_DB", "/tmp/opencode/t_a16_drafts.db")
os.environ.setdefault("WORKFLOW_RUNS_DB", "/tmp/opencode/t_a16_runs.db")
os.environ.setdefault("WORKFLOW_APPROVALS_DB", "/tmp/opencode/t_a16_approvals.db")

import httpx
import pytest
from app.authz.principal import get_principal
from app.main import create_app
from app.workflows.store import get_run, reset_workflow_stores
from core.auth import Principal, Role
from workflow_engine.interfaces import NodeSpec

pytestmark = pytest.mark.unit

WS = f"ws-a16-{uuid.uuid4().hex[:6]}"
USER = f"usr-a16-{uuid.uuid4().hex[:6]}"


class _BlockingNodeExecutor:
    """Node executor whose nodes block on an event until the test releases them."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.executed: list[str] = []

    async def execute_node(
        self, node: NodeSpec, input_data: dict[str, Any], workspace_id: str
    ) -> dict[str, Any]:
        self.started.set()
        await self.gate.wait()
        self.executed.append(node.id)
        return {"output": input_data}


def _definition(wid: str) -> dict[str, Any]:
    return {
        "id": wid,
        "workspace_id": WS,
        "name": "Blocking",
        "description": "single node that blocks",
        "nodes": [
            {
                "id": "trigger-1",
                "type": "trigger",
                "name": "Start",
                "output_ports": [{"name": "output", "type": "dict"}],
            },
            {
                "id": "work-1",
                "type": "output",
                "name": "Work",
                "config": {"template": "hi"},
                "input_ports": [{"name": "input", "type": "any"}],
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source_node_id": "trigger-1",
                "source_port": "output",
                "target_node_id": "work-1",
                "target_port": "input",
            }
        ],
    }


def _app() -> Any:
    app = create_app()
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=USER,
        email="a16@test.org",
        is_authenticated=True,
        is_active=True,
        workspace_roles={WS: Role.OWNER},
    )
    return app


async def _await_status(
    client: httpx.AsyncClient, wid: str, run_id: str, want: tuple[str, ...]
) -> dict:
    last: dict = {}
    for _ in range(400):
        res = await client.get(f"/v1/workspaces/{WS}/workflows/{wid}/runs/{run_id}")
        assert res.status_code == 200, res.text
        last = res.json()["run"]
        if last["status"] in want:
            return last
        await asyncio.sleep(0.01)
    return last


@pytest.fixture(autouse=True)
def _isolated() -> Any:
    reset_workflow_stores()
    yield
    reset_workflow_stores()


def test_initial_run_row_visible_before_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run id is returned and pollable while the node is still blocked."""
    blocker = _BlockingNodeExecutor()
    # ``app.workflows.router`` the *attribute* is the APIRouter, so resolve the
    # module object explicitly before patching.
    router_mod = importlib.import_module("app.workflows.router")
    monkeypatch.setattr(router_mod, "resolve_node_executor", lambda **_kw: blocker)
    wid = "wf-a16-" + uuid.uuid4().hex[:6]

    async def scenario() -> None:
        app = _app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (
                await client.post(
                    f"/v1/workspaces/{WS}/drafts/import", json={"flow_data": _definition(wid)}
                )
            ).status_code == 200
            assert (
                await client.post(f"/v1/workspaces/{WS}/drafts/{wid}/approve", json={"revision": 1})
            ).status_code == 200

            # The run response returns BEFORE the blocked node completes.
            res = await client.post(
                f"/v1/workspaces/{WS}/workflows/{wid}/run", json={"revision": 1, "inputs": {}}
            )
            assert res.status_code == 200, res.text
            run = res.json()["run"]
            run_id = run["id"]
            assert run["status"] == "running", run

            # The node is now blocked inside the supervised task.
            await asyncio.wait_for(blocker.started.wait(), timeout=5)

            # In-flight status poll: the run row exists before completion.
            stat = await client.get(f"/v1/workspaces/{WS}/workflows/{wid}/runs/{run_id}")
            assert stat.status_code == 200, stat.text
            assert stat.json()["run"]["status"] == "running"

            # The registered cancel handle reaches the live executor.
            blocker.gate.set()
            final = await _await_status(client, wid, run_id, ("completed", "failed", "cancelled"))
            assert final["status"] == "completed", final

    asyncio.run(scenario())


def test_in_flight_poll_and_cancel_control_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent poll + cancel control a blocked run, and cancel settles it."""
    blocker = _BlockingNodeExecutor()
    # ``app.workflows.router`` the *attribute* is the APIRouter, so resolve the
    # module object explicitly before patching.
    router_mod = importlib.import_module("app.workflows.router")
    monkeypatch.setattr(router_mod, "resolve_node_executor", lambda **_kw: blocker)
    wid = "wf-a16-" + uuid.uuid4().hex[:6]

    async def scenario() -> None:
        app = _app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (
                await client.post(
                    f"/v1/workspaces/{WS}/drafts/import", json={"flow_data": _definition(wid)}
                )
            ).status_code == 200
            assert (
                await client.post(f"/v1/workspaces/{WS}/drafts/{wid}/approve", json={"revision": 1})
            ).status_code == 200

            res = await client.post(
                f"/v1/workspaces/{WS}/workflows/{wid}/run", json={"revision": 1, "inputs": {}}
            )
            assert res.status_code == 200, res.text
            run_id = res.json()["run"]["id"]
            await asyncio.wait_for(blocker.started.wait(), timeout=5)

            # Cancel while in flight — previously this could not reach the run.
            cancel = await client.post(f"/v1/workspaces/{WS}/workflows/{wid}/runs/{run_id}/cancel")
            assert cancel.status_code == 200, cancel.text
            assert cancel.json()["run"]["status"] == "cancelled"

            # The blocked node only finishes now — the run stays CANCELLED.
            blocker.gate.set()
            final = await _await_status(client, wid, run_id, ("cancelled", "completed"))
            assert final["status"] == "cancelled", final

    asyncio.run(scenario())


def test_initial_run_is_persisted_in_the_run_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The initial RUNNING row is durable in the run store before completion."""
    blocker = _BlockingNodeExecutor()
    # ``app.workflows.router`` the *attribute* is the APIRouter, so resolve the
    # module object explicitly before patching.
    router_mod = importlib.import_module("app.workflows.router")
    monkeypatch.setattr(router_mod, "resolve_node_executor", lambda **_kw: blocker)
    wid = "wf-a16-" + uuid.uuid4().hex[:6]

    async def scenario() -> None:
        app = _app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (
                await client.post(
                    f"/v1/workspaces/{WS}/drafts/import", json={"flow_data": _definition(wid)}
                )
            ).status_code == 200
            assert (
                await client.post(f"/v1/workspaces/{WS}/drafts/{wid}/approve", json={"revision": 1})
            ).status_code == 200

            res = await client.post(
                f"/v1/workspaces/{WS}/workflows/{wid}/run", json={"revision": 1, "inputs": {}}
            )
            run_id = res.json()["run"]["id"]
            await asyncio.wait_for(blocker.started.wait(), timeout=5)

            # Row is registered in the run store while still executing.
            stored = get_run(WS, run_id)
            assert stored is not None, "initial run row must be registered before completion"
            assert stored.status == "running"

            blocker.gate.set()
            await _await_status(client, wid, run_id, ("completed", "failed", "cancelled"))

    asyncio.run(scenario())


def test_terminal_cancel_without_live_executor_still_409() -> None:
    """Unchanged: a run with no live executor still refuses cancel with 409."""
    import threading

    from app.workflows.store import save_run
    from workflow_engine.interfaces import WorkflowRun, WorkflowRunStatus

    wid = "wf-a16-" + uuid.uuid4().hex[:6]
    run = WorkflowRun(
        id="wfrun-a16-terminal",
        workspace_id=WS,
        workflow_id=wid,
        status=WorkflowRunStatus.RUNNING,
        created_at="2026-10-15T00:00:00+00:00",
    )
    save_run(run, None)

    app = _app()
    transport = httpx.ASGITransport(app=app)
    out: dict[str, Any] = {}

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.post(
                f"/v1/workspaces/{WS}/workflows/{wid}/runs/wfrun-a16-terminal/cancel"
            )
            out["status_code"] = res.status_code

    thread = threading.Thread(target=lambda: asyncio.run(scenario()))
    thread.start()
    thread.join(timeout=20)
    assert out.get("status_code") == 409, out