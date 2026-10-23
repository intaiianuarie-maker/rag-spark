"""T-A04b proof tests: checkpoint path fail-closed/restart/bad path."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from apps.api.app.workflows.router import checkpoint_db_path


def test_checkpoint_path_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("WORKFLOW_CHECKPOINT_DB", raising=False)
    with pytest.raises(ValueError, match="WORKFLOW_CHECKPOINT_DB"):
        checkpoint_db_path()

    monkeypatch.setenv("WORKFLOW_CHECKPOINT_DB", "   ")
    with pytest.raises(ValueError, match="WORKFLOW_CHECKPOINT_DB"):
        checkpoint_db_path()


def test_checkpoint_path_bad_unwritable_raises(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_CHECKPOINT_DB", "/no/such/parent/dir/checkpoints.db")
    # resolve must succeed (path only); store startup fails when creating parent dir fails
    from workflow_engine.checkpoint import SqliteCheckpointStore

    blocker = Path("/tmp/opencode/t_a04b_blocker")
    blocker.write_text("x")
    with pytest.raises(OSError):
        SqliteCheckpointStore(blocker / "checkpoints.db")


def test_checkpoint_restart_read(monkeypatch) -> None:
    d = tempfile.mkdtemp()
    db = os.path.join(d, "cp.db")
    monkeypatch.setenv("WORKFLOW_CHECKPOINT_DB", db)

    from workflow_engine.checkpoint import SqliteCheckpointStore, WorkflowCheckpoint

    store = SqliteCheckpointStore(Path(db))
    import asyncio

    async def _write() -> None:
        await store.append(
            WorkflowCheckpoint(
                run_id="run-1",
                workflow_id="wf",
                revision_id="rev",
                workspace_id="ws",
                status="completed",
                boundary="terminal",
                initial_input={},
                completed_nodes=["n1"],
                node_outputs={"n1": {"output": "ok"}},
                node_runs={},
                revision_hash="h",
                checkpointed_at="now",
            )
        )

    asyncio.run(_write())
    # fresh store
    store2 = SqliteCheckpointStore(Path(db))
    cp = asyncio.run(store2.latest("run-1"))
    assert cp is not None and cp.completed_nodes == ["n1"]
