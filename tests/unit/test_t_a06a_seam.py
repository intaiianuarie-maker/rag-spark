"""Accessors seam tests."""

from __future__ import annotations


def test_modes(monkeypatch) -> None:
    import apps.api.app.workflows.store as storemod
    vars = ("WORKFLOW_DRAFT_DB", "WORKFLOW_RUNS_DB",
             "WORKFLOW_APPROVALS_DB", "WORKFLOW_CHECKPOINT_DB")
    for var in vars:
        monkeypatch.delenv(var, raising=False)
    assert storemod.draft_store_mode() == "in-memory"
    assert storemod.runs_store_mode() == "in-memory"
    assert storemod.approvals_store_mode() == "in-memory"
    assert storemod.checkpoint_db_mode() == "unset"
    monkeypatch.setenv("WORKFLOW_DRAFT_DB", "   ")
    assert storemod.draft_store_mode() == "in-memory"
    monkeypatch.setenv("WORKFLOW_DRAFT_DB", "/tmp/x")
    assert storemod.draft_store_mode() == "persistent"
