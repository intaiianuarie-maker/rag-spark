from __future__ import annotations

import sqlite3

import pytest


@pytest.mark.unit
def test_append_only_trigger_rejects_update(tmp_path) -> None:
    path = tmp_path / "draft.db"
    conn = sqlite3.connect(str(path))
    migration = "packages/workflow_engine/migrations/0001_workflow_draft_revisions.up.sql"
    with open(migration, encoding="utf-8") as f:
        sql = f.read()
    conn.executescript(sql)
    conn.commit()
    conn.execute(
        "INSERT INTO workflow_draft_revisions "
        "(workspace_id, workflow_id, revision, content_hash, spec, summary, created_at) "
        "VALUES ('w','f',1,"
        "'0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',"
        "'{}','summary','now');"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE workflow_draft_revisions SET revision = 2 WHERE revision = 1;")
        conn.commit()
    conn.close()


@pytest.mark.unit
def test_draft_store_fail_closed_on_unset_env(monkeypatch) -> None:
    import apps.api.app.workflows.store as storemod

    monkeypatch.delenv("WORKFLOW_DRAFT_DB", raising=False)
    with pytest.raises(ValueError):
        storemod.draft_db_path()


@pytest.mark.unit
def test_store_mode_disclosure_both_configs(monkeypatch) -> None:
    import apps.api.app.workflows.store as storemod

    monkeypatch.delenv("WORKFLOW_DRAFT_DB", raising=False)
    assert storemod.draft_store_mode() == "in-memory"
    monkeypatch.setenv("WORKFLOW_DRAFT_DB", "/tmp/x")
    assert storemod.draft_store_mode() == "persistent"
