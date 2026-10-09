"""Unit tests for Phase 13 Workflow Engine."""

from collections.abc import AsyncIterator

import pytest
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider
from workflow_engine import (
    NodeType,
    Workflow,
    WorkflowEdge,
    WorkflowEngine,
    WorkflowNode,
    WorkflowRunStatus,
)


class MockWorkflowLLM(LLMProvider):
    provider_name = "mock_wf"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        return GenerationResult(
            text=f"Classified: {request.messages[-1].content.upper()}",
            model=request.model,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        yield (await self.generate(request)).text


@pytest.mark.unit
async def test_linear_workflow_execution() -> None:
    engine = WorkflowEngine()

    wf = Workflow(
        id="wf-linear",
        workspace_id="ws-1",
        name="Linear Ingestion Pipeline",
        nodes=[
            WorkflowNode(id="n1", type=NodeType.TRIGGER, name="Start"),
            WorkflowNode(
                id="n2",
                type=NodeType.TRANSFORM,
                name="Format",
                config={"template": "Hello, {name}!"},
            ),
            WorkflowNode(id="n3", type=NodeType.OUTPUT, name="Finish"),
        ],
        edges=[
            WorkflowEdge(id="e1", source_node_id="n1", target_node_id="n2"),
            WorkflowEdge(id="e2", source_node_id="n2", target_node_id="n3"),
        ],
    )

    run = await engine.run(wf, initial_input={"name": "Alice"})

    assert run.status == WorkflowRunStatus.COMPLETED
    assert run.final_output == "Hello, Alice!"
    assert len(run.node_runs) == 3
    assert run.node_runs["n1"].output_data == {"name": "Alice"}
    assert run.node_runs["n2"].output_data == "Hello, Alice!"
    assert run.node_runs["n3"].output_data == "Hello, Alice!"


@pytest.mark.unit
async def test_conditional_branching_workflow() -> None:
    engine = WorkflowEngine()

    # Workflow: Document uploaded -> Condition (is_legal?)
    # -> True: Legal Output / False: General Output
    wf = Workflow(
        id="wf-branch",
        workspace_id="ws-1",
        name="Conditional Document Router",
        nodes=[
            WorkflowNode(id="start", type=NodeType.TRIGGER),
            WorkflowNode(
                id="cond",
                type=NodeType.CONDITION,
                config={"field": "doc_type", "expected": "legal"},
            ),
            WorkflowNode(id="legal_out", type=NodeType.OUTPUT, name="Legal Output"),
            WorkflowNode(id="general_out", type=NodeType.OUTPUT, name="General Output"),
        ],
        edges=[
            WorkflowEdge(id="e1", source_node_id="start", target_node_id="cond"),
            WorkflowEdge(
                id="e_true",
                source_node_id="cond",
                target_node_id="legal_out",
                condition_value=True,
            ),
            WorkflowEdge(
                id="e_false",
                source_node_id="cond",
                target_node_id="general_out",
                condition_value=False,
            ),
        ],
    )

    # Branch 1: legal document
    run_legal = await engine.run(wf, initial_input={"doc_type": "legal", "text": "NDA Contract"})
    assert run_legal.status == WorkflowRunStatus.COMPLETED
    assert "legal_out" in run_legal.node_runs
    assert "general_out" not in run_legal.node_runs

    # Branch 2: general document
    run_general = await engine.run(wf, initial_input={"doc_type": "marketing", "text": "Blog Post"})
    assert run_general.status == WorkflowRunStatus.COMPLETED
    assert "general_out" in run_general.node_runs
    assert "legal_out" not in run_general.node_runs


@pytest.mark.unit
async def test_workflow_with_llm_node() -> None:
    llm = MockWorkflowLLM()
    engine = WorkflowEngine(llm_provider=llm)

    wf = Workflow(
        id="wf-llm",
        workspace_id="ws-llm",
        name="LLM Classification",
        nodes=[
            WorkflowNode(id="start", type=NodeType.TRIGGER),
            WorkflowNode(
                id="classifier",
                type=NodeType.LLM,
                config={"system_prompt": "Classify text", "model": "gpt-4o-mini"},
            ),
            WorkflowNode(id="finish", type=NodeType.OUTPUT),
        ],
        edges=[
            WorkflowEdge(id="e1", source_node_id="start", target_node_id="classifier"),
            WorkflowEdge(id="e2", source_node_id="classifier", target_node_id="finish"),
        ],
    )

    run = await engine.run(wf, initial_input={"text": "invoice"})
    assert run.status == WorkflowRunStatus.COMPLETED
    assert "Classified" in run.final_output
