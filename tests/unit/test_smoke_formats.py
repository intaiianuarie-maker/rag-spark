"""Unit test for T-SMOKE-FORMATS: Ingest 5 file types + retrieve smoke test."""

from __future__ import annotations

import pytest

from evals.smoke_formats_eval import run_smoke_formats_battery

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_smoke_formats_ingest_and_retrieve_5_types() -> None:
    results = await run_smoke_formats_battery()
    assert results["formats_tested"] == 5
    assert results["formats_successful"] == 5
    for _fmt, detail in results["details"].items():
        assert detail["elements_count"] > 0
        assert detail["chunks_count"] > 0
        assert detail["retrieval_matched"] is True
