from __future__ import annotations

import pytest

from evals.eval_v4_workflow_live_scenarios import run_scenario_battery


@pytest.mark.asyncio
async def test_v4_workflow_live_scenario_battery() -> None:
    results = await run_scenario_battery()
    assert results["validation_valid"] is True
    assert results["compile_steps"] > 0
    assert results["execution_status"] == "completed"
    assert results["nodes_executed"] == 4
    assert results["checkpoint_found"] is True
    assert "Gemma verified analysis answer." in str(results["final_output"])
