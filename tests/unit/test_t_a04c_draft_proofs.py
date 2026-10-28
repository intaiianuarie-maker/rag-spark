
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from workflow_engine.draft_store import InMemoryDraftStore, WorkflowDefinition
from workflow_engine.interfaces import NodeSpec, NodeType, PortSpec


def _definition(wid, ws):
    return WorkflowDefinition(
        id=wid,
        workspace_id=ws,
        name="d",
        nodes=[
            NodeSpec(
                id="t", type=NodeType.TRIGGER, name="T",
                output_ports=[PortSpec(name="output", type="dict")],
            )
        ],
        edges=[],
    )


def test_draft_bad_path_refuses(tmp_path):
    import apps.api.app.workflows.store as storemod

    os.environ.pop("WORKFLOW_DRAFT_DB", None)
    with pytest.raises(ValueError):
        storemod.draft_db_path()

    blocker = tmp_path / "t_a04c_blocker"
    blocker.write_text("x")
    import sqlite3

    from stores.workflow_draft_store import SqliteDraftStore
    with pytest.raises(sqlite3.OperationalError):
        SqliteDraftStore(blocker / "drafts.db")
    blocker.unlink(missing_ok=True)


def test_draft_restart_read_and_parity():
    async def _run():
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "drafts.db"
            os.environ["WORKFLOW_DRAFT_DB"] = str(db)

            from stores.workflow_draft_store import SqliteDraftStore
            ws = "ws-1"
            wid = "wf-proof"
            defn = _definition(wid, ws)

            first = SqliteDraftStore(db)
            rev = await first.save_draft(defn)
            assert rev is not None
            hist = await first.history(ws, wid)
            cnt = await first.count(ws, wid)

            second = SqliteDraftStore(db)
            rev2 = await second.latest(ws, wid)
            assert rev2 is not None
            assert rev2.revision.revision == rev.revision.revision
            assert rev2.content_hash == rev.content_hash
            assert len(await second.history(ws, wid)) == len(hist)
            assert await second.count(ws, wid) == cnt

            mem = InMemoryDraftStore()
            memrev = await mem.save_draft(defn)
            assert memrev.revision.revision == rev.revision.revision

    asyncio.run(_run())
