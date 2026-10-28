from __future__ import annotations

import asyncio

from packages.stores.agent_stores import (
    get_queue,
    reset_all_stores,
    set_agent_store_db,
)


def test_sqlite_agent_store_roundtrip(tmp_path) -> None:
    db = tmp_path / "agent.db"
    reset_all_stores()
    set_agent_store_db(db)
    # queue
    q = get_queue()

    async def main() -> None:
        await q.enqueue("run1")
        assert await q.depth() == 1
        cl = await q.claim(5.0)
        assert cl is not None and str(cl) == "run1"
        assert await q.ack(cl) is True

    asyncio.run(main())
    # reopen in fresh process simulation: file exists
    assert db.exists()


def test_unset_keeps_inmemory(monkeypatch, tmp_path) -> None:
    reset_all_stores()
    monkeypatch.delenv("AGENT_STORE_DB", raising=False)
    # no seam call; defaults in-memory
    from packages.stores.agent_stores import get_run_store

    assert "InMemory" in type(get_run_store()).__name__
    assert "InMemory" in type(get_run_store()).__name__


def test_sqlite_queue_reopen(tmp_path) -> None:
    import asyncio

    from packages.stores.agent_stores import SqliteAgentRunQueue

    db = tmp_path / "q.db"
    q1 = SqliteAgentRunQueue(db)
    asyncio.run(q1.enqueue("run-reopen"))
    q2 = SqliteAgentRunQueue(db)
    assert asyncio.run(q2.depth()) == 1
