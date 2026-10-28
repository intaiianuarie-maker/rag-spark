from __future__ import annotations

import sqlite3

import pytest


@pytest.mark.unit
def test_checkpoint_stale_writer_raises(tmp_path) -> None:
    import asyncio

    from workflow_engine.checkpoint import (
        CheckpointSequenceError,
        SqliteCheckpointStore,
        WorkflowCheckpoint,
    )

    store = SqliteCheckpointStore(str(tmp_path / "cp.db"))

    async def main() -> None:
        cp1 = WorkflowCheckpoint(
            run_id="r", workflow_id="w", revision_id="rev", workspace_id="ws", sequence=0
        )
        await store.append(cp1)
        cp2 = WorkflowCheckpoint(
            run_id="r", workflow_id="w", revision_id="rev", workspace_id="ws", sequence=999
        )
        with pytest.raises(CheckpointSequenceError):
            await store.append(cp2)

    asyncio.run(main())


@pytest.mark.unit
def test_migration_null_parent_refused(tmp_path) -> None:

    conn = sqlite3.connect(str(tmp_path / "d.db"))
    sql = open(
        "packages/workflow_engine/migrations/0001_workflow_draft_revisions.up.sql", encoding="utf-8"
    ).read()
    conn.executescript(sql)
    conn.commit()
    conn.execute(
        "INSERT INTO workflow_draft_revisions "
        "(workspace_id, workflow_id, revision, content_hash, spec, summary, created_at) "
        "VALUES (?, ?, 1, ?, '{}', 's', 'now');",
        ("w", "f", "0" * 64),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO workflow_draft_revisions "
            "(workspace_id, workflow_id, revision, content_hash, "
            "spec, summary, parent_revision, parent_hash, created_at) "
            "VALUES (?, ?, 2, ?, '{}', 's', NULL, NULL, 'now');",
            ("w", "f", "0123456789abcdef" * 4),
        )
        conn.commit()
    conn.close()
