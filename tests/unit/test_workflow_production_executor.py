"""Unit tests for T-OR-15: Production Workflow Execution with Model & Verification Nodes."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from models.interfaces import GenerationResult
from workflow_engine import (
    NodeSpec,
    NodeType,
    PortSpec,
)
from workflow_engine.manifest import CapabilityNotSupportedError
from workflow_engine.production_executor import ProductionNodeExecutor

pytestmark = [pytest.mark.unit]


def _make_node(
    node_id: str,
    node_type: NodeType,
    config: dict | None = None,
    caps: list[str] | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        config=config or {},
        required_capabilities=caps or [],
        input_ports=[PortSpec(name="input", type="any")],
        output_ports=[PortSpec(name="output", type="any")],
    )


@pytest.mark.asyncio
async def test_production_executor_executes_gemma_llm_node() -> None:
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = GenerationResult(
        text="Gemma workflow output response.",
        model="google/gemma-4-26b-a4b-it:free",
    )

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-1",
        NodeType.LLM,
        config={
            "model": "google/gemma-4-26b-a4b-it:free",
            "system_prompt": "You are a test assistant.",
        },
        caps=["llm"],
    )

    result = await executor.execute_node(node, {"text": "Process this data"}, "ws-test")
    assert result["output"] == "Gemma workflow output response."
    assert mock_provider.generate.called
    req = mock_provider.generate.call_args[0][0]
    assert req.model == "google/gemma-4-26b-a4b-it:free"
    assert req.messages[1].content == "Process this data"


@pytest.mark.asyncio
async def test_production_executor_executes_gemma_paid_fallback_model() -> None:
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = GenerationResult(
        text="Paid Gemma response.",
        model="google/gemma-4-26b-a4b-it",
    )

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-paid",
        NodeType.LLM,
        config={"model": "google/gemma-4-26b-a4b-it"},
        caps=["llm"],
    )

    result = await executor.execute_node(node, "summarize", "ws-test")
    assert result["output"] == "Paid Gemma response."


@pytest.mark.asyncio
async def test_production_executor_refuses_unadmitted_model() -> None:
    mock_provider = AsyncMock()
    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-unadmitted",
        NodeType.LLM,
        config={"model": "unapproved/forbidden-model-xyz"},
        caps=["llm"],
    )

    with pytest.raises(CapabilityNotSupportedError, match="not admitted for workflow role"):
        await executor.execute_node(node, "prompt", "ws-test")
    assert not mock_provider.generate.called


@pytest.mark.asyncio
async def test_production_executor_evidence_verification_flagging() -> None:
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = GenerationResult(
        text="Verified evidence analysis.",
        model="google/gemma-4-26b-a4b-it:free",
    )

    executor = ProductionNodeExecutor(llm_provider=mock_provider)
    node = _make_node(
        "llm-verified",
        NodeType.LLM,
        config={"model": "google/gemma-4-26b-a4b-it:free", "requires_verification": True},
        caps=["llm"],
    )

    result = await executor.execute_node(node, "check facts", "ws-test")
    assert result["output"] == "Verified evidence analysis."
    assert result["verified"] is True
    assert result["verification_stage"] == "Stage 11 & 12"


@pytest.mark.asyncio
async def test_production_executor_deterministic_primitives() -> None:
    executor = ProductionNodeExecutor()

    # Trigger
    trig = _make_node("t1", NodeType.TRIGGER)
    assert await executor.execute_node(trig, {"k": "v"}, "ws-1") == {"output": {"k": "v"}}

    trans = _make_node("tr1", NodeType.TRANSFORM, config={"template": "Hello {name}!"})
    res_trans = await executor.execute_node(trans, {"name": "World"}, "ws-1")
    assert res_trans == {"output": "Hello World!"}

    # Condition
    cond = _make_node("c1", NodeType.CONDITION, config={"field": "status", "expected": "active"})
    assert await executor.execute_node(cond, {"status": "active"}, "ws-1") == {
        "result": True,
        "output": {"status": "active"},
    }

    # Output
    out = _make_node("o1", NodeType.OUTPUT)
    assert await executor.execute_node(out, "final value", "ws-1") == {"output": "final value"}
