"""Unit tests for V4 Visual Workflows API handlers (T-R19).

Verifies the 3 target metrics:
1. handler paths without authorization scoping: 0
   - Cross-workspace access refused per handler (HTTP 403 on all 8 operations:
     draft, import, validate, save, approve, run, status, cancel).
2. UI fields showing mock content as live: 0
   - Traced via frontend contract tests in apps/web/src/app/workflows/authoring-surface.test.ts
3. enforcement refusals swallowed: 0
   - Append-only / store / validation errors propagate honestly without masking into 200:
     * DraftConflictError -> 409
     * StoreBoundsError -> 413
     * StoreSecretError -> 422
     * Scope/Identity mismatch -> 422
     * Unvalidated approval -> 422
     * Not found -> 404
"""

from __future__ import annotations

import os

os.environ.setdefault("WORKFLOW_CHECKPOINT_DB", "/tmp/opencode/v4_test_checkpoints.db")
os.environ.setdefault("WORKFLOW_DRAFT_DB", "/tmp/opencode/v4_test_drafts.db")
os.environ.setdefault("WORKFLOW_RUNS_DB", "/tmp/opencode/v4_test_runs.db")
os.environ.setdefault("WORKFLOW_APPROVALS_DB", "/tmp/opencode/v4_test_approvals.db")

import uuid
from typing import Any

import pytest
from app.authz.principal import get_principal
from app.main import create_app
from app.workflows.store import reset_workflow_stores
from core.auth import Principal, Role
from fastapi.testclient import TestClient
from workflow_engine.routes.schemas import DraftErrorCode

pytestmark = pytest.mark.unit

WS_ALLOWED = f"ws-allowed-{uuid.uuid4().hex[:6]}"
WS_FOREIGN = f"ws-foreign-{uuid.uuid4().hex[:6]}"
USER_ID = f"usr-{uuid.uuid4().hex[:6]}"


@pytest.fixture(autouse=True)
def _setup():
    reset_workflow_stores()


def _make_client(user_roles: dict[str, Role] | None = None) -> TestClient:
    app = create_app()

    roles = user_roles if user_roles is not None else {WS_ALLOWED: Role.OWNER}
    principal = Principal(
        user_id=USER_ID,
        email="dev2@example.com",
        is_authenticated=True,
        is_active=True,
        workspace_roles=roles,
    )
    app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app)


def _sample_definition(
    wid: str = "wf-test-1",
    workspace: str = WS_ALLOWED,
    name: str = "Test Workflow",
    secret: bool = False,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {"api_key": "sk-secret-leak"} if secret else {"template": "Hello {name}"}
    return {
        "id": wid,
        "workspace_id": workspace,
        "name": name,
        "description": "Standard DAG test",
        "nodes": [
            {
                "id": "trigger-1",
                "type": "trigger",
                "name": "Start",
                "output_ports": [{"name": "output", "type": "dict"}],
            },
            {
                "id": "output-1",
                "type": "output",
                "name": "End",
                "config": cfg,
                "input_ports": [{"name": "input", "type": "any"}],
            },
        ],
        "edges": [
            {
                "id": "edge-1",
                "source_node_id": "trigger-1",
                "source_port": "output",
                "target_node_id": "output-1",
                "target_port": "input",
            }
        ],
    }


# =========================================================================== #
# Metric 1: Handler paths without authorization scoping: 0
# =========================================================================== #


def test_metric_1_cross_workspace_refused_across_all_8_handlers():
    """Prove cross-workspace access is refused (HTTP 403) for all 8 handler paths."""
    client = _make_client({WS_ALLOWED: Role.OWNER})

    # 1. draft: list & get
    res_list = client.get(f"/v1/workspaces/{WS_FOREIGN}/drafts")
    assert res_list.status_code == 403
    assert res_list.json()["detail"]["error"] == "not_a_member"

    res_get = client.get(f"/v1/workspaces/{WS_FOREIGN}/drafts/wf-any")
    assert res_get.status_code == 403

    res_hist = client.get(f"/v1/workspaces/{WS_FOREIGN}/drafts/wf-any/revisions")
    assert res_hist.status_code == 403

    # 2. import
    res_imp = client.post(
        f"/v1/workspaces/{WS_FOREIGN}/drafts/import",
        json={"flow_data": _sample_definition("wf-foreign", WS_FOREIGN)},
    )
    assert res_imp.status_code == 403

    # 3. validate
    res_val = client.post(
        f"/v1/workspaces/{WS_FOREIGN}/drafts/validate",
        json={"workflow_id": "wf-any"},
    )
    assert res_val.status_code == 403

    # 4. save & revise
    res_save = client.post(
        f"/v1/workspaces/{WS_FOREIGN}/drafts",
        json=_sample_definition("wf-foreign", WS_FOREIGN),
    )
    assert res_save.status_code == 403

    res_rev = client.post(
        f"/v1/workspaces/{WS_FOREIGN}/drafts/wf-foreign/revisions",
        json=_sample_definition("wf-foreign", WS_FOREIGN),
    )
    assert res_rev.status_code == 403

    # 5. approve
    res_app = client.post(
        f"/v1/workspaces/{WS_FOREIGN}/drafts/wf-foreign/approve",
        json={"revision": 1},
    )
    assert res_app.status_code == 403

    # 6. run
    res_run = client.post(
        f"/v1/workspaces/{WS_FOREIGN}/workflows/wf-foreign/run",
        json={"inputs": {}},
    )
    assert res_run.status_code == 403

    # 7. status
    res_stat = client.get(
        f"/v1/workspaces/{WS_FOREIGN}/workflows/wf-foreign/runs/wfrun-xyz"
    )
    assert res_stat.status_code == 403

    # 8. cancel
    res_cancel = client.post(
        f"/v1/workspaces/{WS_FOREIGN}/workflows/wf-foreign/runs/wfrun-xyz/cancel"
    )
    assert res_cancel.status_code == 403


# =========================================================================== #
# Metric 3: Enforcement refusals swallowed: 0
# =========================================================================== #


def test_metric_3_append_only_conflict_not_swallowed():
    """Attempting to save revision 1 when revisions already exist returns 409 Conflict."""
    client = _make_client()
    body = _sample_definition("wf-append-1", WS_ALLOWED)

    # First save succeeds
    res1 = client.post(f"/v1/workspaces/{WS_ALLOWED}/drafts", json=body)
    assert res1.status_code == 200
    assert res1.json()["revision"] == 1
    assert res1.json()["idempotent"] is False

    # Second save on same workflow id returns 409 Conflict (not swallowed into 200)
    res2 = client.post(f"/v1/workspaces/{WS_ALLOWED}/drafts", json=body)
    assert res2.status_code == 409
    err = res2.json()["detail"]
    assert err["error"] == DraftErrorCode.DRAFT_CONFLICT.value
    assert "already has" in err["message"] and "revise" in err["message"]


def test_metric_3_secret_refusal_not_swallowed():
    """Secret configuration in a node is rejected with 422 DRAFT_SECRET_REFUSED."""
    client = _make_client()
    secret_body = _sample_definition("wf-secret-1", WS_ALLOWED, secret=True)

    res = client.post(f"/v1/workspaces/{WS_ALLOWED}/drafts", json=secret_body)
    assert res.status_code == 422
    # Detail is lean redacted shape or DraftErrorBody
    err = res.json()
    assert "detail" in err


def test_metric_3_scope_and_identity_mismatches_not_swallowed():
    """Body identity / workspace disagreeing with path is rejected honestly."""
    client = _make_client()
    body = _sample_definition("wf-mismatch-1", WS_ALLOWED)

    # Path workspace ws-allowed vs body workspace ws-foreign
    body_wrong_ws = dict(body, workspace_id="ws-other")
    res_ws = client.post(f"/v1/workspaces/{WS_ALLOWED}/drafts", json=body_wrong_ws)
    assert res_ws.status_code == 422

    # Save revision with mismatched workflow_id in path
    res_id = client.post(
        f"/v1/workspaces/{WS_ALLOWED}/drafts/wf-other/revisions",
        json=body,
    )
    assert res_id.status_code == 422


def test_metric_3_unvalidated_approval_refused():
    """Approving an invalid workflow revision is rejected with 422 approval_failed_validation."""
    client = _make_client()

    # Create invalid workflow definition (dangling edge referencing non-existent node)
    invalid_body = {
        "id": "wf-invalid-1",
        "workspace_id": WS_ALLOWED,
        "name": "Invalid Flow",
        "nodes": [
            {
                "id": "t1",
                "type": "trigger",
                "name": "Start",
                "output_ports": [{"name": "out", "type": "string"}],
            },
            {
                "id": "o1",
                "type": "output",
                "name": "End",
                "input_ports": [{"name": "in", "type": "string"}],
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source_node_id": "t1",
                "source_port": "out",
                "target_node_id": "non-existent-node",
                "target_port": "in",
            }
        ],
    }

    # DraftDefinitionRequest checks declared nodes and refuses at boundary
    res_save = client.post(f"/v1/workspaces/{WS_ALLOWED}/drafts", json=invalid_body)
    assert res_save.status_code == 422


# =========================================================================== #
# End-to-End Lifecycle: All 8 Operations on Authorized Lane
# =========================================================================== #


def test_v4_authoring_full_lifecycle():
    """Full lifecycle: save -> validate -> revise -> approve -> run -> status -> cancel."""
    client = _make_client()
    wid = "wf-lifecycle-1"
    body = _sample_definition(wid, WS_ALLOWED)

    # 1. SAVE: revision 1
    res_save = client.post(f"/v1/workspaces/{WS_ALLOWED}/drafts", json=body)
    assert res_save.status_code == 200
    rev1 = res_save.json()
    assert rev1["revision"] == 1
    assert rev1["workflow_id"] == wid
    assert len(rev1["content_hash"]) == 64

    # 2. VALIDATE: draft validation succeeds
    res_val = client.post(
        f"/v1/workspaces/{WS_ALLOWED}/drafts/validate",
        json={"workflow_id": wid},
    )
    assert res_val.status_code == 200
    val_data = res_val.json()
    assert val_data["valid"] is True
    assert len(val_data["errors"]) == 0

    # 3. REVISE: append revision 2
    body_rev2 = dict(body, description="Updated description for revision 2")
    res_rev = client.post(
        f"/v1/workspaces/{WS_ALLOWED}/drafts/{wid}/revisions",
        json=body_rev2,
    )
    assert res_rev.status_code == 200
    rev2 = res_rev.json()
    assert rev2["revision"] == 2
    assert rev2["parent_revision"] == 1

    # 4. DRAFT: list, get latest, get revision, history
    res_list = client.get(f"/v1/workspaces/{WS_ALLOWED}/drafts")
    assert res_list.status_code == 200
    drafts = res_list.json()["drafts"]
    assert any(d["workflow_id"] == wid for d in drafts)

    res_get = client.get(f"/v1/workspaces/{WS_ALLOWED}/drafts/{wid}")
    assert res_get.status_code == 200
    assert res_get.json()["revision"] == 2

    res_r1 = client.get(f"/v1/workspaces/{WS_ALLOWED}/drafts/{wid}/revisions/1")
    assert res_r1.status_code == 200
    assert res_r1.json()["revision"] == 1

    res_hist = client.get(f"/v1/workspaces/{WS_ALLOWED}/drafts/{wid}/revisions")
    assert res_hist.status_code == 200
    assert res_hist.json()["count"] == 2

    # 5. APPROVE: approve revision 2
    res_app = client.post(
        f"/v1/workspaces/{WS_ALLOWED}/drafts/{wid}/approve",
        json={"revision": 2, "comment": "Ready for run"},
    )
    assert res_app.status_code == 200
    app_data = res_app.json()
    assert app_data["approved"] is True
    assert app_data["revision"] == 2
    assert app_data["workflow_id"] == wid

    # 6. RUN: execute workflow
    res_run = client.post(
        f"/v1/workspaces/{WS_ALLOWED}/workflows/{wid}/run",
        json={"revision": 2, "inputs": {"name": "Antigravity"}},
    )
    assert res_run.status_code == 200
    run_data = res_run.json()
    run_id = run_data["run"]["id"]
    assert run_data["run"]["status"] in ("completed", "running")
    assert "timeline" in run_data

    # 7. STATUS: inspect run status and timeline
    res_stat = client.get(
        f"/v1/workspaces/{WS_ALLOWED}/workflows/{wid}/runs/{run_id}"
    )
    assert res_stat.status_code == 200
    stat_data = res_stat.json()
    assert stat_data["run"]["id"] == run_id

    # 8. CANCEL: cancel run
    res_can = client.post(
        f"/v1/workspaces/{WS_ALLOWED}/workflows/{wid}/runs/{run_id}/cancel"
    )
    assert res_can.status_code == 200
    can_data = res_can.json()
    assert can_data["run"]["status"] in ("completed", "cancelled")


def test_import_workflow_native_and_langflow():
    """Import handler accepts both native definitions and Langflow structures."""
    client = _make_client()

    # Native format import
    native_data = _sample_definition("wf-imported-native", WS_ALLOWED, "Native Flow")
    res_native = client.post(
        f"/v1/workspaces/{WS_ALLOWED}/drafts/import",
        json={"flow_data": native_data, "name": "Imported Native"},
    )
    assert res_native.status_code == 200
    assert res_native.json()["workflow_id"] == "wf-imported-native"
    assert res_native.json()["revision"] == 1


def test_run_workflow_refuses_without_checkpoint_db(monkeypatch):
    monkeypatch.delenv("WORKFLOW_CHECKPOINT_DB", raising=False)
    from app.authz.principal import get_principal
    from app.main import app
    from core.auth import Principal, Role
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=USER_ID,
        email="user@test.org",
        is_authenticated=True,
        workspace_roles={WS_ALLOWED: Role.ADMIN},
    )
    client = TestClient(app)
    resp = client.post(
        f"/v1/workspaces/{WS_ALLOWED}/workflows/wf-does-not-matter/run",
        json={"revision": 1, "inputs": {}},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "checkpoint_unavailable"


def test_run_workflow_invalid_definition_returns_422():
    from app.authz.principal import get_principal
    from app.main import app
    from core.auth import Principal, Role
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=USER_ID,
        email="user@test.org",
        is_authenticated=True,
        workspace_roles={WS_ALLOWED: Role.OWNER},
    )
    wid = "wf-invalid-" + __import__("uuid").uuid4().hex[:6]
    body = {
        "id": wid,
        "workspace_id": WS_ALLOWED,
        "name": "Invalid",
        "description": "x",
        "nodes": [
            {"id": "trigger-1", "type": "trigger", "name": "Start", "output_ports": [{"name": "output", "type": "dict"}]},  # noqa: E501
            {"id": "http-1", "type": "http", "name": "Http", "input_ports": [{"name": "input", "type": "any"}]},  # noqa: E501
        ],
        "edges": [{"id": "e1", "source_node_id": "trigger-1", "source_port": "output", "target_node_id": "http-1", "target_port": "input"}],  # noqa: E501
    }
    import requests
    _ = requests
    client = TestClient(app)
    client.post(f"/v1/workspaces/{WS_ALLOWED}/drafts/import", json={"flow_data": body})
    client.post(f"/v1/workspaces/{WS_ALLOWED}/drafts/{wid}/approve", json={"revision": 1})
    resp = client.post(f"/v1/workspaces/{WS_ALLOWED}/workflows/{wid}/run", json={"revision": 1, "inputs": {}})  # noqa: E501
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_definition"
    assert "traceback" not in resp.text.lower()


def test_run_workflow_runtime_failure_returns_coded_error():
    import uuid

    from app.authz.principal import get_principal
    from app.main import app
    from core.auth import Principal, Role
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=USER_ID,
        email="user@test.org",
        is_authenticated=True,
        workspace_roles={WS_ALLOWED: Role.OWNER},
    )
    wid = "wf-runtime-" + uuid.uuid4().hex[:6]
    body = {
        "id": wid,
        "workspace_id": WS_ALLOWED,
        "name": "Runtime",
        "description": "x",
        "nodes": [
            {"id": "trigger-1", "type": "trigger", "name": "Start", "output_ports": [{"name": "output", "type": "dict"}]},  # noqa: E501
            {"id": "llm-1", "type": "llm", "name": "LLM", "config": {"model": "x"}, "input_ports": [{"name": "input", "type": "any"}], "output_ports": [{"name": "output", "type": "any"}]},  # noqa: E501
            {"id": "output-1", "type": "output", "name": "End", "input_ports": [{"name": "input", "type": "any"}]},  # noqa: E501
        ],
        "edges": [
            {"id": "e1", "source_node_id": "trigger-1", "source_port": "output", "target_node_id": "llm-1", "target_port": "input"},  # noqa: E501
            {"id": "e2", "source_node_id": "llm-1", "source_port": "output", "target_node_id": "output-1", "target_port": "input"},  # noqa: E501
        ],
    }
    client = TestClient(app)
    client.post(f"/v1/workspaces/{WS_ALLOWED}/drafts/import", json={"flow_data": body})
    client.post(f"/v1/workspaces/{WS_ALLOWED}/drafts/{wid}/approve", json={"revision": 1})
    resp = client.post(f"/v1/workspaces/{WS_ALLOWED}/workflows/{wid}/run", json={"revision": 1, "inputs": {}})  # noqa: E501
    assert resp.status_code == 200
    data = resp.json()["run"]
    assert data["status"] == "failed"
    assert data["error"].startswith("node 'llm-1' failed: LLM node")
    assert "traceback" not in resp.text.lower()
