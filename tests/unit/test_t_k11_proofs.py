"""T-K11 proof tests."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents.worker import AgentWorker


@pytest.mark.asyncio
async def test_park_failure_warning(caplog) -> None:
    caplog.set_level(logging.WARNING)
    worker = AgentWorker.__new__(AgentWorker)
    worker.queue = MagicMock()
    claim_obj = MagicMock()
    worker.queue.claim = AsyncMock(return_value=claim_obj)
    worker.context_store = MagicMock()
    worker.context_store.get.return_value = None
    worker.queue.park = AsyncMock(side_effect=RuntimeError("boom"))
    worker.runtime = MagicMock()
    await worker.run_once()
    assert any("agent_queue_park_failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_recover_failure_surfaced(caplog) -> None:
    caplog.set_level(logging.WARNING)
    worker = AgentWorker.__new__(AgentWorker)
    worker.queue = MagicMock()
    worker.queue.recover_expired = AsyncMock(side_effect=RuntimeError("boom"))
    worker.queue.claim = AsyncMock(return_value=None)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(worker.run_forever(lease_seconds=0.0), timeout=0.05)
    assert any("agent_queue_recover_failed" in rec.message for rec in caplog.records)


def test_prefix_fallback_not_test_colon(monkeypatch) -> None:
    from agents.queue import _prefix

    with patch("core.config.get_settings", side_effect=RuntimeError("boom")):
        monkeypatch.delenv("AGENT_QUEUE_PREFIX", raising=False)
        prefix = _prefix()
    assert prefix != "test:" and not prefix.startswith("test:")
