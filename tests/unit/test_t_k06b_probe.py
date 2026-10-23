from __future__ import annotations

import sqlite3

from app.workflows.store import migration_applied


def test_migration_applied_true(tmp_path) -> None:
    p = tmp_path / "draft.db"
    conn = sqlite3.connect(str(p))
    migration_path = "packages/workflow_engine/migrations/0001_workflow_draft_revisions.up.sql"
    with open(migration_path, encoding="utf-8") as f:
        sql = f.read()
    conn.executescript(sql)
    conn.commit()
    conn.close()
    assert migration_applied(str(p)) is True


def test_migration_applied_false_unapplied(tmp_path) -> None:
    p = tmp_path / "empty.db"
    sqlite3.connect(str(p)).close()
    assert migration_applied(str(p)) is False


def test_migration_applied_missing(tmp_path) -> None:
    assert migration_applied(str(tmp_path / "nope.db")) is False


def test_migration_applied_corrupt(tmp_path) -> None:
    p = tmp_path / "bad.db"
    p.write_bytes(b"not a database")
    assert migration_applied(str(p)) is False
