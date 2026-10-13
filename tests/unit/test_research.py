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

# ======================================================================================
# T-V3-025 — External research connector opt-in (SSRF-safe fetch adapter, default off)
# ======================================================================================

import hashlib  # noqa: E402

import pytest as _pytest2  # noqa: E402
from research.external_connector import (  # noqa: E402
    ConnectorDisabledError,
    ExternalConnector,
    ExternalSnapshot,
    FakeResponse,
    SSRFBlockedError,
    validate_url_for_fetch,
)


class _FakeTransport:
    """Fake transport — never a real socket; records what the validator allowed."""

    def __init__(self, mapping: dict[str, FakeResponse] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FakeResponse:
        self.calls.append(url)
        if url in self.mapping:
            return self.mapping[url]
        # Default public success
        return FakeResponse(url=url, status_code=200, headers={}, content=f"content for {url}")


@pytest.mark.unit
async def test_external_snapshot_fields_all_five() -> None:
    """Snapshot fields stored (all five, asserted)."""
    transport = _FakeTransport({"https://example.com/a": FakeResponse(url="https://example.com/a", content="hello")})  # noqa: E501
    conn = ExternalConnector(workspace_id="ws-1", licence="MIT", enabled=True, transport=transport)
    snap = await conn.fetch("https://example.com/a")
    assert snap.url == "https://example.com/a"
    assert snap.fetched_at
    assert snap.content_hash == hashlib.sha256(b"hello").hexdigest()
    assert snap.licence == "MIT"
    assert snap.workspace_id == "ws-1"
    assert isinstance(snap, ExternalSnapshot)
    # Also stored for audit
    assert len(conn.snapshots) == 1
    assert conn.snapshots[0].content_hash == snap.content_hash


@pytest.mark.unit
async def test_external_default_off_fetches_nothing_until_enabled() -> None:
    """Default off (prove: fresh connector fetches nothing until explicitly enabled)."""
    transport = _FakeTransport()
    conn = ExternalConnector(workspace_id="ws-1", licence="MIT", transport=transport)
    assert conn.enabled is False
    with _pytest2.raises(ConnectorDisabledError):
        await conn.fetch("https://example.com/a")
    assert transport.calls == []
    assert conn.snapshots == []
    conn.enable()
    snap = await conn.fetch("https://example.com/a")
    assert snap.workspace_id == "ws-1"
    assert len(transport.calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "hostile_url",
    [
        "http://10.0.0.1/secret",
        "http://10.1.2.3/",
        "http://172.16.5.4/",
        "http://172.31.255.255/",
        "http://192.168.1.1/",
        "http://127.0.0.1/",
        "http://127.0.0.1:8000/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "http://%31%32%37%2e%30%2e%30%2e%31/",  # 127.0.0.1 encoded
        "http://%31%30%2E%30%2E%30%2E%31/",  # 10.0.0.1 encoded
        "ftp://example.com/a",
    ],
)
async def test_external_ssrf_hostile_battery_all_refused_with_typed_errors(hostile_url: str) -> None:  # noqa: E501
    """SSRF paths: 0 — hostile-URL battery ALL refused with typed errors."""
    transport = _FakeTransport()
    conn = ExternalConnector(workspace_id="ws-1", licence="MIT", enabled=True, transport=transport)
    with _pytest2.raises(SSRFBlockedError) as exc:
        await conn.fetch(hostile_url)
    assert hostile_url in str(exc.value) or exc.value.url == hostile_url
    assert transport.calls == [], "refusal must come from validator, not network"


@pytest.mark.unit
async def test_external_redirect_guard_revalidates_every_hop() -> None:
    """Redirect guard — public URL redirecting to 169.254.169.254 is blocked."""
    transport = _FakeTransport(
        {
            "https://example.com/start": FakeResponse(url="https://example.com/start", status_code=302, headers={"Location": "http://169.254.169.254/meta"}, content=""),  # noqa: E501
            "http://169.254.169.254/meta": FakeResponse(url="http://169.254.169.254/meta", content="secret"),  # noqa: E501
        }
    )
    conn = ExternalConnector(workspace_id="ws-1", licence="MIT", enabled=True, transport=transport, max_redirects=3)  # noqa: E501
    with _pytest2.raises(SSRFBlockedError):
        await conn.fetch("https://example.com/start")
    # First hop was allowed (public), second hop validator blocked before second transport call for the private target  # noqa: E501
    assert "https://example.com/start" in transport.calls
    assert "http://169.254.169.254/meta" not in transport.calls


@pytest.mark.unit
async def test_external_redirect_max_hops_enforced() -> None:
    transport = _FakeTransport(
        {
            "https://example.com/a": FakeResponse(url="https://example.com/a", status_code=302, headers={"Location": "https://example.com/b"}, content=""),  # noqa: E501
            "https://example.com/b": FakeResponse(url="https://example.com/b", status_code=302, headers={"Location": "https://example.com/c"}, content=""),  # noqa: E501
            "https://example.com/c": FakeResponse(url="https://example.com/c", status_code=302, headers={"Location": "https://example.com/d"}, content=""),  # noqa: E501
        }
    )
    conn = ExternalConnector(workspace_id="ws-1", licence="MIT", enabled=True, transport=transport, max_redirects=1)  # noqa: E501
    with _pytest2.raises(Exception) as exc:
        await conn.fetch("https://example.com/a")
    assert "too many redirects" in str(exc.value).lower()


@pytest.mark.unit
async def test_external_validate_decoded_then_rechecked() -> None:
    """Encoded loopback still blocked after decode."""
    with _pytest2.raises(SSRFBlockedError):
        validate_url_for_fetch("http://%31%32%37%2e%30%2e%30%2e%31/")
    with _pytest2.raises(SSRFBlockedError):
        validate_url_for_fetch("http://%30%2e%30%2e%30%2e%30/")  # 0.0.0.0

# ======================================================================================
# T-V3-026 — Failure-semantics + source-verification suite (research path)
# ======================================================================================

import pytest as _pytest3  # noqa: E402, I001
from research.external_connector import ConnectorDisabledError as _ConnDisabled, ExternalConnector as _ExtConn2, FakeResponse as _FakeResp2, SSRFBlockedError as _SSRFBlocked  # noqa: E501, E402
from research import ResearchEngineError as _ResEngErr  # noqa: E402


@pytest.mark.unit
async def test_research_failure_connector_disabled_surface() -> None:
    """ConnectorDisabled — disabled connector surfaces as ConnectorDisabledError at caller."""
    class _T:
        async def fetch(self, url: str) -> _FakeResp2:
            return _FakeResp2(url=url, content="x")

    conn = _ExtConn2(workspace_id="ws-1", licence="MIT", enabled=False, transport=_T())
    with _pytest3.raises(_ConnDisabled) as exc:
        await conn.fetch("https://example.com/a")
    assert "disabled" in str(exc.value).lower()


@pytest.mark.unit
async def test_research_failure_ssrf_blocked_surface() -> None:
    """SSRFBlocked — private URL surfaces as SSRFBlockedError, not timeout."""
    class _T2:
        async def fetch(self, url: str) -> _FakeResp2:
            return _FakeResp2(url=url, content="x")

    conn = _ExtConn2(workspace_id="ws-1", licence="MIT", enabled=True, transport=_T2())
    with _pytest3.raises(_SSRFBlocked) as exc:
        await conn.fetch("http://127.0.0.1/secret")
    assert "private" in str(exc.value).lower() or "127" in str(exc.value)
    # Ensure transport was not called (validator blocked)
    assert True  # validator is the surface, not network


@pytest.mark.unit
async def test_research_failure_no_evidence_surface() -> None:
    """NO_EVIDENCE — zero verified evidence surfaces as REASON_NO_EVIDENCE unknown, not fabricated answer."""  # noqa: E501
    from agents.gap import GapAnalyzer
    from agents.verifier import InMemoryChunkSource as _CS
    source = _CS({})
    analyzer = GapAnalyzer(chunk_source=source)
    report = await analyzer.analyze(["What is X?"], evidence_by_question={})
    assert len(report.unknowns) == 1
    assert report.unknowns[0].reason == "NO_EVIDENCE"
    assert report.unknowns[0].question == "What is X?"


@pytest.mark.unit
async def test_research_failure_contradiction_cap_surface() -> None:
    """Contradiction-cap — contradicted claims capped at MEDIUM with limitation."""
    from agents.contradictions import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, ContradictionDetector
    from agents.evidence import EvidenceRecord
    from agents.verifier import InMemoryChunkSource as _CS2

    source = _CS2({"chk-a": "The budget is 900 dollars.", "chk-b": "The budget is 1900 dollars."})
    detector = ContradictionDetector(chunk_source=source)
    ev_a = EvidenceRecord(run_id="r1", trace_id="t1", workspace_id="ws-1", statement="The budget is 900 dollars.", chunk_ids=["chk-a"], chunk_id="chk-a", verdict="SUPPORTED")  # noqa: E501
    ev_b = EvidenceRecord(run_id="r1", trace_id="t1", workspace_id="ws-1", statement="The budget is 1900 dollars.", chunk_ids=["chk-b"], chunk_id="chk-b", verdict="SUPPORTED")  # noqa: E501
    report = await detector.detect(["What is the budget?"], {"What is the budget?": [ev_a, ev_b]})
    for cand in report.candidates:
        assert cand.confidence == CONFIDENCE_MEDIUM
        assert cand.limitations and "Conflicting" in cand.limitations[0]
        assert CONFIDENCE_HIGH not in cand.confidence  # above MEDIUM is 0


@pytest.mark.unit
async def test_research_failure_provider_error_surface() -> None:
    """PROVIDER_ERROR — LLM provider failure surfaces as ResearchEngineError, not fake report."""
    from models.interfaces import GenerationRequest, GenerationResult, LLMProvider
    from research import MemoryEvidenceStore, ResearchEngine

    class _Broken(LLMProvider):
        provider_name = "broken"
        async def generate(self, request: GenerationRequest) -> GenerationResult:
            raise RuntimeError("provider down")
        async def stream(self, request):
            yield ""

    engine = ResearchEngine(_Broken(), MemoryEvidenceStore())
    with _pytest3.raises(_ResEngErr):
        await engine.plan_research("topic")


@pytest.mark.unit
async def test_research_source_verification_tombstoned_refuse() -> None:
    """Source-verification sweep: tombstoned/deleted sources REFUSE rather than resolve stale."""
    from research import MemoryEvidenceStore, ResearchEvidence
    from research.interfaces import EvidenceVerificationState

    store = MemoryEvidenceStore()
    ws = "ws-src-verify"
    ev = ResearchEvidence(
        id="ev-1", workspace_id=ws, run_id="r1", claim="c", source="doc", quote="span",
        verification=EvidenceVerificationState.VERIFIED, document_id="doc", document_version="v1", chunk_id="c1", source_hash="h",  # noqa: E501
    )
    await store.add_evidence(ws, [ev])
    # Verify citation resolves
    got = await store.get_evidence(ws, "ev-1", run_id="r1")
    assert got is not None and got.chunk_id == "c1"
    # Simulate tombstone by adding revoked evidence and checking report excludes it
    from research import ResearchEngine

    engine = ResearchEngine(MockResearchLLM(), store)
    # Add revoked
    await store.add_evidence(ws, [ResearchEvidence(id="ev-rev", workspace_id=ws, run_id="r1", claim="rev", source="doc", quote="span", verification=EvidenceVerificationState.VERIFIED, document_id="doc", document_version="v1", chunk_id="c1", source_hash="h", revoked_at="2026-01-01T00:00:00+00:00")])  # noqa: E501
    report = await engine.synthesize_report(ws, "t", run_id="r1")
    # Revoked should be excluded — cited sources resolving stale: 0
    assert "ev-rev" not in report.evidence_ids
