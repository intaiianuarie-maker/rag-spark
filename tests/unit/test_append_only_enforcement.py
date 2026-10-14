"""Comprehensive unit tests for append-only enforcement, verify-on-read,
and approval bindings (T-R18).

Proves the 3 target metrics:
1. mutating paths still succeeding: 0
   - Exact F13 trio proved refused with typed errors: UPDATE, DELETE, backwards/out-of-order INSERT.
   - Refused at raw SQLite trigger layer and at Python store layer.
2. reads without hash verification: 0
   - Verify-on-read wired on get(), latest(), and history().
   - Tamper detected and rejected with DraftTamperError.
3. approval bindings missing hash/policy: 0
   - WorkflowApprovalBinding strictly binds workspace + hash + policy.
   - Incomplete bindings refused with ApprovalBindingError.
   - Approval check fails if hash, workspace, or policy differs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from stores import (
    ApprovalBindingError,
    DraftMutationError,
    DraftNotFoundError,
    DraftTamperError,
    SqliteDraftStore,
    SqliteWorkflowApprovalStore,
    WorkflowApprovalBinding,
)
from workflow_engine.interfaces import NodeSpec, NodeType, WorkflowDefinition

pytestmark = pytest.mark.unit

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2] / "packages" / "workflow_engine" / "migrations"
)
UP_SQL = MIGRATIONS_DIR / "0001_workflow_draft_revisions.up.sql"


def _create_sample_workflow(
    workspace_id: str, workflow_id: str = "wf-test", name: str = "Test Workflow"
) -> WorkflowDefinition:
    return WorkflowDefinition(
        id=workflow_id,
        workspace_id=workspace_id,
        name=name,
        description="Append-only validation workflow",
        nodes=[NodeSpec(id="trigger_1", type=NodeType.TRIGGER)],
        workspace_capabilities={"retrieval", "llm"},
    )


# =========================================================================== #
# Metric 1: Mutating paths still succeeding: 0 (Exact F13 Trio)
# =========================================================================== #


def test_f13_raw_sql_update_is_refused_by_trigger(tmp_path: Path) -> None:
    """Proves F13 trio #1: UPDATE is blocked with typed error at SQLite trigger boundary."""
    db_path = tmp_path / "f13_update.db"
    _ = SqliteDraftStore(db_path=db_path)
    con = sqlite3.connect(db_path)

    # Insert a valid revision 1
    con.execute(
        """
        INSERT INTO workflow_draft_revisions (
            workspace_id, workflow_id, revision, content_hash, spec, summary,
            parent_revision, parent_hash, created_at
        ) VALUES ('ws-f13', 'wf-1', 1, ?, '{}', 'summary-v1', NULL, NULL, '2026-10-14T00:00:00Z')
        """,
        ("a" * 64,),
    )
    con.commit()

    # Attempting UPDATE must be refused with typed error
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)) as exc_info:
        con.execute(
            "UPDATE workflow_draft_revisions SET summary = 'tampered' WHERE workflow_id = 'wf-1'"
        )
    assert "append-only: UPDATE prohibited" in str(exc_info.value)
    con.close()


def test_f13_raw_sql_delete_is_refused_by_trigger(tmp_path: Path) -> None:
    """Proves F13 trio #2: DELETE is blocked with typed error at SQLite trigger boundary."""
    db_path = tmp_path / "f13_delete.db"
    _ = SqliteDraftStore(db_path=db_path)
    con = sqlite3.connect(db_path)

    con.execute(
        """
        INSERT INTO workflow_draft_revisions (
            workspace_id, workflow_id, revision, content_hash, spec, summary,
            parent_revision, parent_hash, created_at
        ) VALUES ('ws-f13', 'wf-1', 1, ?, '{}', 'summary-v1', NULL, NULL, '2026-10-14T00:00:00Z')
        """,
        ("a" * 64,),
    )
    con.commit()

    # Attempting DELETE must be refused with typed error
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)) as exc_info:
        con.execute("DELETE FROM workflow_draft_revisions WHERE workflow_id = 'wf-1'")
    assert "append-only: DELETE prohibited" in str(exc_info.value)
    con.close()


def test_f13_raw_sql_backwards_and_out_of_order_insert_refused_by_trigger(tmp_path: Path) -> None:
    """Proves F13 trio #3: Out-of-order and backwards insert blocked at SQLite trigger boundary."""
    db_path = tmp_path / "f13_insert.db"
    _ = SqliteDraftStore(db_path=db_path)
    con = sqlite3.connect(db_path)

    # 1. Insert revision 1
    con.execute(
        """
        INSERT INTO workflow_draft_revisions (
            workspace_id, workflow_id, revision, content_hash, spec, summary,
            parent_revision, parent_hash, created_at
        ) VALUES ('ws-f13', 'wf-1', 1, ?, '{}', 'summary-v1', NULL, NULL, '2026-10-14T00:00:00Z')
        """,
        ("a" * 64,),
    )
    con.commit()

    # 2. Try inserting revision 3 (skipping revision 2) -> out-of-order insertion
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)) as exc_info:
        con.execute(
            """
            INSERT INTO workflow_draft_revisions (
                workspace_id, workflow_id, revision, content_hash, spec, summary,
                parent_revision, parent_hash, created_at
            ) VALUES ('ws-f13', 'wf-1', 3, ?, '{}', 'summary-v3', 1, ?, '2026-10-14T00:00:00Z')
            """,
            ("c" * 64, "a" * 64),
        )
    assert "out-of-order or backwards revision insertion prohibited" in str(exc_info.value)

    # 3. Insert valid revision 2
    con.execute(
        """
        INSERT INTO workflow_draft_revisions (
            workspace_id, workflow_id, revision, content_hash, spec, summary,
            parent_revision, parent_hash, created_at
        ) VALUES ('ws-f13', 'wf-1', 2, ?, '{}', 'summary-v2', 1, ?, '2026-10-14T00:00:00Z')
        """,
        ("b" * 64, "a" * 64),
    )
    con.commit()

    # 4. Try backwards insert of revision 1 again or revision 2 again
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
        con.execute(
            """
            INSERT INTO workflow_draft_revisions (
                workspace_id, workflow_id, revision, content_hash, spec, summary,
                parent_revision, parent_hash, created_at
            ) VALUES (
                'ws-f13', 'wf-1', 1, ?, '{}', 'backwards-v1', NULL, NULL, '2026-10-14T00:00:00Z'
            )
            """,
            ("d" * 64,),
        )
    con.close()


@pytest.mark.asyncio
async def test_store_interface_blocks_mutating_methods() -> None:
    """Proves mutating methods are strictly absent/refused on SqliteDraftStore."""
    store = SqliteDraftStore()
    with pytest.raises(DraftMutationError, match="append-only: update is prohibited"):
        store.update()
    with pytest.raises(DraftMutationError, match="append-only: delete is prohibited"):
        store.delete()
    with pytest.raises(DraftMutationError, match="append-only: remove is prohibited"):
        store.remove()
    with pytest.raises(DraftMutationError, match="append-only: overwrite is prohibited"):
        store.overwrite()
    with pytest.raises(DraftMutationError, match="append-only: purge is prohibited"):
        store.purge()


# =========================================================================== #
# Metric 2: Reads without hash verification: 0 (Verify-on-Read)
# =========================================================================== #


@pytest.mark.asyncio
async def test_valid_reads_verify_content_hashes_cleanly(tmp_path: Path) -> None:
    """Prove normal reads verify-on-read successfully with byte-for-byte fidelity."""
    db_path = tmp_path / "valid_reads.db"
    store = SqliteDraftStore(db_path=db_path)
    ws_id = "ws-valid"
    wf_def = _create_sample_workflow(ws_id, "wf-1")

    rev1 = await store.save_draft(wf_def)
    assert rev1.number == 1
    assert len(rev1.content_hash) == 64

    # Read back via get()
    fetched = await store.get(ws_id, "wf-1", 1)
    assert fetched.content_hash == rev1.content_hash
    assert fetched.spec.name == "Test Workflow"

    # Read back via latest()
    latest_rec = await store.latest(ws_id, "wf-1")
    assert latest_rec is not None
    assert latest_rec.content_hash == rev1.content_hash

    # Read back via history()
    history = await store.history(ws_id, "wf-1")
    assert len(history) == 1
    assert history[0].content_hash == rev1.content_hash


@pytest.mark.asyncio
async def test_verify_on_read_detects_tamper_in_get(tmp_path: Path) -> None:
    """Prove get() immediately raises DraftTamperError when spec is tampered in storage."""
    db_path = tmp_path / "tamper_get.db"
    store = SqliteDraftStore(db_path=db_path)
    ws_id = "ws-tamper"
    wf_def = _create_sample_workflow(ws_id, "wf-get")
    await store.save_draft(wf_def)

    # Directly tamper storage: change node id inside spec JSON
    # Note: SQLite triggers block UPDATE on workflow_draft_revisions, so to simulate
    # storage corruption or disk tamper, we drop the trigger temporarily on a separate connection
    raw_con = sqlite3.connect(db_path)
    raw_con.execute("DROP TRIGGER trg_wdr_no_update")
    cur = raw_con.execute("SELECT spec FROM workflow_draft_revisions WHERE workflow_id = 'wf-get'")
    orig_spec = cur.fetchone()[0]
    tampered_spec = orig_spec.replace("trigger_1", "malicious_trigger_tamper")
    raw_con.execute(
        "UPDATE workflow_draft_revisions SET spec = ? WHERE workflow_id = 'wf-get'",
        (tampered_spec,),
    )
    raw_con.commit()
    raw_con.close()

    # Reading the tampered revision via get() MUST raise DraftTamperError
    with pytest.raises(DraftTamperError) as exc_info:
        await store.get(ws_id, "wf-get", 1)
    assert "tamper detected" in str(exc_info.value)
    assert "content_hash mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_verify_on_read_detects_tamper_in_latest(tmp_path: Path) -> None:
    """Prove latest() immediately raises DraftTamperError when stored hash is tampered."""
    db_path = tmp_path / "tamper_latest.db"
    store = SqliteDraftStore(db_path=db_path)
    ws_id = "ws-tamper"
    wf_def = _create_sample_workflow(ws_id, "wf-latest")
    await store.save_draft(wf_def)

    raw_con = sqlite3.connect(db_path)
    raw_con.execute("DROP TRIGGER trg_wdr_no_update")
    raw_con.execute(
        "UPDATE workflow_draft_revisions SET content_hash = ? WHERE workflow_id = 'wf-latest'",
        ("f" * 64,),
    )
    raw_con.commit()
    raw_con.close()

    with pytest.raises(DraftTamperError) as exc_info:
        await store.latest(ws_id, "wf-latest")
    assert "tamper detected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_verify_on_read_detects_tamper_in_history(tmp_path: Path) -> None:
    """Prove history() raises DraftTamperError if any revision in history is tampered."""
    db_path = tmp_path / "tamper_history.db"
    store = SqliteDraftStore(db_path=db_path)
    ws_id = "ws-tamper"
    wf_def = _create_sample_workflow(ws_id, "wf-hist")
    await store.save_draft(wf_def)

    # Append revision 2
    wf_def_v2 = _create_sample_workflow(ws_id, "wf-hist", name="Updated Workflow")
    await store.revise(ws_id, "wf-hist", wf_def_v2)

    # Tamper revision 1 in storage
    raw_con = sqlite3.connect(db_path)
    raw_con.execute("DROP TRIGGER trg_wdr_no_update")
    raw_con.execute(
        "UPDATE workflow_draft_revisions SET content_hash = ? "
        "WHERE workflow_id = 'wf-hist' AND revision = 1",
        ("0" * 64,),
    )
    raw_con.commit()
    raw_con.close()

    with pytest.raises(DraftTamperError) as exc_info:
        await store.history(ws_id, "wf-hist")
    assert "tamper detected" in str(exc_info.value)


# =========================================================================== #
# Metric 3: Approval bindings missing hash/policy: 0
# =========================================================================== #


def test_approval_binding_model_enforces_mandatory_fields() -> None:
    """Prove WorkflowApprovalBinding refuses bindings missing workspace, hash, or policy."""
    valid_hash = "e" * 64

    # 1. Valid binding passes
    binding = WorkflowApprovalBinding(
        workspace_id="ws-1",
        workflow_id="wf-1",
        revision=1,
        content_hash=valid_hash,
        policy_revision="pol-rev-1",
        approver_id="user-1",
    )
    assert binding.status == "approved"

    # 2. Missing/empty workspace_id refused
    with pytest.raises(ApprovalBindingError, match="missing required workspace_id"):
        WorkflowApprovalBinding(
            workspace_id="",
            workflow_id="wf-1",
            revision=1,
            content_hash=valid_hash,
            policy_revision="pol-rev-1",
            approver_id="user-1",
        )

    # 3. Missing/invalid content_hash refused
    with pytest.raises(ApprovalBindingError, match="content_hash must be 64-char"):
        WorkflowApprovalBinding(
            workspace_id="ws-1",
            workflow_id="wf-1",
            revision=1,
            content_hash="short",
            policy_revision="pol-rev-1",
            approver_id="user-1",
        )

    # 4. Missing policy_revision refused
    with pytest.raises(ApprovalBindingError, match="missing required policy_revision"):
        WorkflowApprovalBinding(
            workspace_id="ws-1",
            workflow_id="wf-1",
            revision=1,
            content_hash=valid_hash,
            policy_revision="",
            approver_id="user-1",
        )


@pytest.mark.asyncio
async def test_approval_store_binds_workspace_hash_policy_exact(tmp_path: Path) -> None:
    """Prove approval check strictly verifies workspace, content_hash, and policy_revision."""
    db_path = tmp_path / "approval.db"
    store = SqliteWorkflowApprovalStore(db_path=db_path)

    ws_id = "ws-appr"
    wf_id = "wf-1"
    rev = 1
    content_hash_1 = "a" * 64
    policy_rev_1 = "rev1"
    approver = "alice"

    # 1. Bind approval
    binding = await store.bind_approval(
        workspace_id=ws_id,
        workflow_id=wf_id,
        revision=rev,
        content_hash=content_hash_1,
        policy_revision=policy_rev_1,
        approver_id=approver,
    )
    assert binding.approver_id == "alice"

    # 2. Exact match succeeds
    assert await store.is_approved(
        workspace_id=ws_id,
        workflow_id=wf_id,
        revision=rev,
        content_hash=content_hash_1,
        policy_revision=policy_rev_1,
    ) is True

    # 3. Workspace mismatch fails (no cross-workspace leak)
    assert await store.is_approved(
        workspace_id="ws-other",
        workflow_id=wf_id,
        revision=rev,
        content_hash=content_hash_1,
        policy_revision=policy_rev_1,
    ) is False

    # 4. Content hash mismatch fails (tampered/modified workflow requires new approval)
    tampered_hash = "b" * 64
    assert await store.is_approved(
        workspace_id=ws_id,
        workflow_id=wf_id,
        revision=rev,
        content_hash=tampered_hash,
        policy_revision=policy_rev_1,
    ) is False

    # 5. Policy revision mismatch fails (bumped policy rev2 requires re-approval)
    assert await store.is_approved(
        workspace_id=ws_id,
        workflow_id=wf_id,
        revision=rev,
        content_hash=content_hash_1,
        policy_revision="rev2",
    ) is False


def test_approval_bindings_persistence_is_immutable(tmp_path: Path) -> None:
    """Prove approval audit bindings table cannot be UPDATEd or DELETEd directly."""
    db_path = tmp_path / "approval_immut.db"
    _ = SqliteWorkflowApprovalStore(db_path=db_path)
    con = sqlite3.connect(db_path)

    con.execute(
        """
        INSERT INTO workflow_approval_bindings (
            workspace_id, workflow_id, revision, content_hash, policy_revision,
            approver_id, status, approved_at
        ) VALUES ('ws-1', 'wf-1', 1, ?, 'rev1', 'admin', 'approved', '2026-10-14T00:00:00Z')
        """,
        ("a" * 64,),
    )
    con.commit()

    # UPDATE is prohibited
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)) as exc_info:
        con.execute("UPDATE workflow_approval_bindings SET status = 'rejected'")
    assert "workflow_approval_bindings is append-only: UPDATE prohibited" in str(exc_info.value)

    # DELETE is prohibited
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)) as exc_info:
        con.execute("DELETE FROM workflow_approval_bindings")
    assert "workflow_approval_bindings is append-only: DELETE prohibited" in str(exc_info.value)
    con.close()


# =========================================================================== #
# Workspace Isolation Proof
# =========================================================================== #


@pytest.mark.asyncio
async def test_workspace_isolation_in_sqlite_draft_store(tmp_path: Path) -> None:
    """Prove draft store enforces workspace boundaries: cross-workspace lookups return not found."""
    db_path = tmp_path / "isolation.db"
    store = SqliteDraftStore(db_path=db_path)

    # Save wf-shared in ws-alpha
    wf_alpha = _create_sample_workflow("ws-alpha", "wf-shared", name="Alpha Shared")
    await store.save_draft(wf_alpha)

    # Save wf-shared in ws-beta
    wf_beta = _create_sample_workflow("ws-beta", "wf-shared", name="Beta Shared")
    await store.save_draft(wf_beta)

    # Each workspace sees only its own revision 1
    rec_alpha = await store.get("ws-alpha", "wf-shared", 1)
    rec_beta = await store.get("ws-beta", "wf-shared", 1)
    assert rec_alpha.spec.name == "Alpha Shared"
    assert rec_beta.spec.name == "Beta Shared"

    # Cross-workspace read of unknown ID in scope returns not found
    with pytest.raises(DraftNotFoundError):
        await store.get("ws-gamma", "wf-shared", 1)

    # Listing workflows per workspace is isolated
    assert await store.workflow_ids("ws-alpha") == ["wf-shared"]
    assert await store.workflow_ids("ws-gamma") == []
