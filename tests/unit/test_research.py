"""Unit tests for Phase 12 Research Engine and Evidence Store."""

import json
from collections.abc import AsyncIterator

import pytest
from models.interfaces import GenerationRequest, GenerationResult, LLMProvider
from research import (
    EvidenceStoreError,
    EvidenceVerificationState,
    MemoryEvidenceStore,
    ResearchEngine,
    ResearchEngineError,
    ResearchEvidence,
    ResearchModelPolicy,
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

    # Step 2: Evidence accumulation — verified against a real stored chunk.
    ev = await engine.add_evidence_item(
        workspace_id=ws,
        query_id="q-raft",
        claim="Raft achieves consensus via leader election and log replication.",
        source="paper-raft",
        quote="Raft decomposes consensus into leader election, log replication, and safety.",
        tags=["consensus", "protocols"],
        chunk_ids=["chunk-raft-1"],
        document_id="paper-raft",
        document_version="v1",
        chunk_id="chunk-raft-1",
        quote_start=0,
        quote_end=69,
        source_hash="sha256:aa11",
        verification=EvidenceVerificationState.VERIFIED,
        verification_reason="quote resolves to chunk-raft-1 in paper-raft v1",
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


@pytest.mark.unit
async def test_report_abstains_without_verified_evidence() -> None:
    """rew1 B5: zero verified evidence must never produce a COMPLETED report."""
    llm = MockResearchLLM()
    store = MemoryEvidenceStore()
    engine = ResearchEngine(llm, store)
    ws = "ws-abstain"

    # Caller-supplied prose is UNVERIFIED by default.
    await engine.add_evidence_item(
        workspace_id=ws,
        query_id="q-x",
        claim="Supplied by the caller.",
        source="model-output",
        quote="some span",
    )

    report = await engine.synthesize_report(ws, "Nothing")
    assert report.status == ResearchStatus.FAILED
    assert report.supported_findings == []
    assert report.findings == []
    assert "No verified evidence" in report.unknowns[0]


@pytest.mark.unit
async def test_planning_failure_raises_instead_of_faking() -> None:
    """rew1 B5: a provider failure must raise, not return a plausible fallback."""

    class _BrokenLLM(LLMProvider):
        provider_name = "broken"

        async def generate(self, request: GenerationRequest) -> GenerationResult:
            raise RuntimeError("provider down")

        async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
            yield ""

    engine = ResearchEngine(_BrokenLLM(), MemoryEvidenceStore())
    with pytest.raises(ResearchEngineError):
        await engine.plan_research("topic")


@pytest.mark.unit
async def test_synthesis_provider_failure_raises() -> None:
    """rew1 B5: synthesis failure raises; no fake completed report."""

    class _BrokenLLM(LLMProvider):
        provider_name = "broken"

        async def generate(self, request: GenerationRequest) -> GenerationResult:
            if "research planner" in request.messages[-1].content:
                return GenerationResult(
                    text=json.dumps({"subquestions": ["q1"], "scope": "s"}),
                    model=request.model,
                )
            raise RuntimeError("provider down mid-synthesis")

        async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
            yield ""

    engine = ResearchEngine(_BrokenLLM(), MemoryEvidenceStore())
    store = MemoryEvidenceStore()
    engine.store = store
    ws = "ws-synth"
    await engine.add_evidence_item(
        workspace_id=ws,
        query_id="q",
        claim="verified claim",
        source="doc",
        quote="exact quote",
        verification=EvidenceVerificationState.VERIFIED,
        document_id="doc",
        document_version="v1",
        chunk_id="c1",
        source_hash="sha256:bb22",
    )
    with pytest.raises(ResearchEngineError):
        await engine.synthesize_report(ws, "topic")


@pytest.mark.unit
async def test_evidence_run_scoping_blocks_cross_run_reads() -> None:
    """rew1 B6: two runs in one workspace cannot mix evidence."""
    store = MemoryEvidenceStore()
    ws = "ws-runs"
    ev_a = ResearchEvidence(
        id="ev-a",
        workspace_id=ws,
        run_id="run-A",
        claim="A claim",
        source="doc-a",
        quote="span a",
    )
    ev_b = ResearchEvidence(
        id="ev-b",
        workspace_id=ws,
        run_id="run-B",
        claim="B claim",
        source="doc-b",
        quote="span b",
    )
    await store.add_evidence(ws, [ev_a, ev_b])

    run_a = await store.list_evidence(ws, run_id="run-A")
    run_b = await store.list_evidence(ws, run_id="run-B")
    assert [e.id for e in run_a] == ["ev-a"]
    assert [e.id for e in run_b] == ["ev-b"]

    # Cross-run get raises instead of returning the other run's evidence.
    with pytest.raises(EvidenceStoreError):
        await store.get_evidence(ws, "ev-b", run_id="run-A")


@pytest.mark.unit
async def test_cross_workspace_add_raises() -> None:
    """rew1 B7: cross-workspace adds must raise, not silently drop."""
    store = MemoryEvidenceStore()
    ev = ResearchEvidence(
        id="ev-x",
        workspace_id="ws-victim",
        run_id="r1",
        claim="c",
        source="s",
        quote="q",
    )
    with pytest.raises(EvidenceStoreError):
        await store.add_evidence("ws-attacker", [ev])


@pytest.mark.unit
async def test_engine_uses_injected_model_policy() -> None:
    """rew1 follow-up 1: research model selection is policy-driven, not hardcoded."""
    seen: list[str] = []

    class _RecordingLLM(LLMProvider):
        provider_name = "recording"

        async def generate(self, request: GenerationRequest) -> GenerationResult:
            seen.append(request.model)
            return GenerationResult(
                text=json.dumps({"subquestions": ["q1"], "scope": "s"}),
                model=request.model,
            )

        async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
            yield ""

    engine = ResearchEngine(
        _RecordingLLM(),
        MemoryEvidenceStore(),
        model_policy=ResearchModelPolicy(
            planning_model="custom-planner", synthesis_model="custom-synth"
        ),
    )
    await engine.plan_research("t")
    assert seen == ["custom-planner"], "planning must use the injected policy model"


@pytest.mark.unit
async def test_revoked_evidence_excluded_from_report() -> None:
    """rew1 follow-up 3: revoked/deleted evidence must not support a report."""
    store = MemoryEvidenceStore()
    engine = ResearchEngine(MockResearchLLM(), store)
    ws = "ws-revoked"
    await store.add_evidence(
        ws,
        [
            ResearchEvidence(
                id="ev-rev",
                workspace_id=ws,
                run_id="r1",
                claim="revoked claim",
                source="doc",
                quote="span",
                verification=EvidenceVerificationState.VERIFIED,
                document_id="doc",
                document_version="v1",
                chunk_id="c1",
                source_hash="h",
                revoked_at="2026-01-01T00:00:00+00:00",
            )
        ],
    )
    report = await engine.synthesize_report(ws, "t", run_id="r1")
    assert report.status == ResearchStatus.FAILED
    assert report.evidence_ids == []
    assert report.supported_findings == []
    assert any("revoked" in u for u in report.unknowns)
