"""T-A04c fixup proofs."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime

import pytest
from workflow_engine.interfaces import WorkflowRun, WorkflowRunStatus


def test_runs_bad_path_refuses() -> None:
    os.environ.pop("WORKFLOW_RUNS_DB", None)
    import apps.api.app.workflows.store as storemod

    with pytest.raises(ValueError, match="WORKFLOW_RUNS_DB"):
        storemod.runs_db_path()


def test_runs_restart_read() -> None:
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "runs.db")
        os.environ["WORKFLOW_RUNS_DB"] = db
        os.environ["WORKFLOW_APPROVALS_DB"] = os.path.join(d, "approvals.db")

        import apps.api.app.workflows.store as storemod

        ws = "ws-proof"
        run = WorkflowRun(
            id="run-proof",
            workspace_id=ws,
            workflow_id="wf-proof",
            status=WorkflowRunStatus.COMPLETED,
            created_at=datetime.now(UTC).isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
        )
        storemod.save_run(run)
        got = storemod.get_run(ws, "run-proof")
        assert got is not None
        assert got.id == run.id
