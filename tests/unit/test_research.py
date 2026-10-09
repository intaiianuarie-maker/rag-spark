"""Unit tests for Phase 12 Research Engine and Evidence Store."""

import json
from collections.abc import AsyncIterator

import pytest
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider
from research import (
    MemoryEvidenceStore,
    ResearchEngine,
    ResearchEvidence,
    ResearchStatus,
)


class MockResearchLLM(LLMProvider):
    """Deterministic mock LLM for research planning and report generation."""

    provider_name = "mock_research"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        prompt = request.messages[-1].content
        if "research planner" in prompt:
            data = {
                "subquestions": [
                    "What are the latency bounds?",
                    "What are the failure modes?",
                    "How does it compare to alternatives?",
                ],
                "scope": "System architecture performance and reliability",
            }
            return GenerationResult(text=json.dumps(data), model=request.model)
        else:
            # Report generation prompt
            data = {
                "executive_summary": "Comprehensive analysis of system performance.",
                "findings": ["High throughput achieved under normal load."],
                "unknowns": ["Long-term degraded network behavior."],
                "recommendations": ["Implement circuit breakers."],
            }
            return GenerationResult(text=json.dumps(data), model=request.model)

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        yield (await self.generate(request)).text


@pytest.mark.unit
async def test_evidence_store_crud_and_isolation() -> None:
    store = MemoryEvidenceStore()
    ws1 = "ws-res-1"
    ws2 = "ws-res-2"

    ev1 = ResearchEvidence(
        id="ev-1",
        workspace_id=ws1,
        query_id="q-1",
        claim="PostgreSQL handles multi-tenant indexing.",
        source="doc-pg",
        quote="PostgreSQL provides schema-level multi-tenancy.",
        tags=["database", "multi-tenant"],
    )
    ev2 = ResearchEvidence(
        id="ev-2",
        workspace_id=ws1,
        query_id="q-2",
        claim="Qdrant supports isolated collections.",
        source="doc-qdrant",
        quote="Qdrant collections can be segmented by workspace.",
        tags=["vector-db"],
    )

    assert await store.add_evidence(ws1, [ev1, ev2]) == 2
    assert len(await store.list_evidence(ws1)) == 2
    assert len(await store.list_evidence(ws1, query_id="q-1")) == 1

    # Isolation check (R-004)
    assert await store.get_evidence(ws2, "ev-1") is None
    assert len(await store.list_evidence(ws2)) == 0


@pytest.mark.unit
async def test_contradiction_detection() -> None:
    store = MemoryEvidenceStore()
    ws = "ws-contradict"

    ev_pos = ResearchEvidence(
        id="ev-a",
        workspace_id=ws,
        query_id="q-backups",
        claim="The storage cluster supports automated daily backups.",
        source="doc-spec",
        quote="automated daily backups enabled",
        tags=["storage", "backups"],
    )
    ev_neg = ResearchEvidence(
        id="ev-b",
        workspace_id=ws,
        query_id="q-backups",
        claim="The storage cluster cannot perform automated daily backups.",
        source="doc-incident",
        quote="cannot perform automated daily backups due to cron issue",
        tags=["storage", "backups"],
    )

    await store.add_evidence(ws, [ev_pos, ev_neg])
    contradictions = await store.detect_contradictions(ws)

    assert len(contradictions) == 1
    assert contradictions[0].evidence_a_id == "ev-a"
    assert contradictions[0].evidence_b_id == "ev-b"
    assert "automated" in contradictions[0].reason or "backups" in contradictions[0].reason


@pytest.mark.unit
async def test_research_engine_full_pipeline() -> None:
    llm = MockResearchLLM()
    store = MemoryEvidenceStore()
    engine = ResearchEngine(llm, store)
    ws = "ws-pipeline"

    # Step 1: Planning
    plan = await engine.plan_research("Distributed consensus protocols")
    assert len(plan.subquestions) == 3
    assert "latency" in plan.subquestions[0].lower()

    # Step 2: Evidence accumulation
    ev = await engine.add_evidence_item(
        workspace_id=ws,
        query_id="q-raft",
        claim="Raft achieves consensus via leader election and log replication.",
        source="paper-raft",
        quote="Raft decomposes consensus into leader election, log replication, and safety.",
        tags=["consensus", "protocols"],
        chunk_ids=["chunk-raft-1"],
    )
    assert ev.id.startswith("ev-")

    # Step 3: Synthesis
    report = await engine.synthesize_report(ws, "Distributed consensus protocols")
    assert report.status == ResearchStatus.COMPLETED
    assert "analysis of system performance" in report.executive_summary.lower()
    assert len(report.findings) >= 1
    assert "paper-raft" in report.citations
    assert "chunk-raft-1" in ev.chunk_ids
    assert report.evidence_ids == [ev.id]
