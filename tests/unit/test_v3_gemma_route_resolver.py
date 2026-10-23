"""Tests for Gemma route resolution, workspace budget checks, and canonical
citations (T-GEMMA-001).

Covers:
1. Gemma route resolution: free-first with paid fallback after budget check.
2. Workspace budget check: fail-closed when no paid budget.
3. Canonical citations: EvidenceRecord carries source_hash on citations.
4. Restart / cancellation safety: evidence records are idempotent.
5. StormPlanner Gemma route integration.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from agents.evidence import (
    Citation,
    EvidenceRecord,
    InMemoryEvidenceStore,
)
from agents.planner import FREE_MODELS, PlanError, StormPlanner
from agents.policy import (
    GEMMA_APPROVED_MODELS,
    GEMMA_FREE_MODEL,
    GEMMA_PAID_MODEL,
    PolicyViolationError,
    check_workspace_model_budget,
    reset_free_pool_state,
    reset_workspace_model_budget,
    resolve_gemma_route,
    set_free_pool_exhausted,
    set_workspace_model_budget,
)

WS_ID = "00000000-0000-0000-0000-000000000001"
WS_OTHER = "00000000-0000-0000-0000-000000000002"


# ============================================================================
# 1. Gemma Route Resolution Tests
# ============================================================================


class TestResolveGemmaRoute:
    """Test the canonical Gemma route resolver (T-GEMMA-001)."""

    def setup_method(self) -> None:
        """Ensure clean state before each test."""
        reset_free_pool_state()
        reset_workspace_model_budget()

    def teardown_method(self) -> None:
        """Clean up after each test."""
        reset_free_pool_state()
        reset_workspace_model_budget()

    def test_free_pool_available_returns_free_model(self) -> None:
        """When the free pool is available, the free route is selected."""
        result = resolve_gemma_route(WS_ID)
        assert result == GEMMA_FREE_MODEL

    def test_free_pool_exhausted_with_budget_returns_paid_model(self) -> None:
        """When the free pool is exhausted and budget is available, paid route."""
        set_free_pool_exhausted(WS_ID, exhausted=True)
        set_workspace_model_budget(WS_ID, 10.0)

        result = resolve_gemma_route(WS_ID)
        assert result == GEMMA_PAID_MODEL

    def test_free_pool_exhausted_no_budget_raises_policy_violation(self) -> None:
        """When the free pool is exhausted and no budget, fail-closed."""
        set_free_pool_exhausted(WS_ID, exhausted=True)

        with pytest.raises(PolicyViolationError) as exc_info:
            resolve_gemma_route(WS_ID)

        assert exc_info.value.field == "workspace_budget"
        assert "no paid-model budget" in str(exc_info.value)

    def test_free_pool_exhausted_isolation_per_workspace(self) -> None:
        """Free pool exhaustion for one workspace does not affect another."""
        set_free_pool_exhausted(WS_ID, exhausted=True)
        set_workspace_model_budget(WS_ID, 10.0)

        result = resolve_gemma_route(WS_OTHER)
        assert result == GEMMA_FREE_MODEL

    def test_preferred_paid_with_budget(self) -> None:
        """When preferred='paid' and budget is available, paid route is selected."""
        set_workspace_model_budget(WS_ID, 10.0)

        result = resolve_gemma_route(WS_ID, preferred="paid")
        assert result == GEMMA_PAID_MODEL

    def test_preferred_paid_no_budget_raises(self) -> None:
        """When preferred='paid' and no budget, fail-closed."""
        with pytest.raises(PolicyViolationError) as exc_info:
            resolve_gemma_route(WS_ID, preferred="paid")

        assert exc_info.value.field == "workspace_budget"

    def test_preferred_invalid_raises(self) -> None:
        """Invalid preferred value raises PolicyViolationError."""
        with pytest.raises(PolicyViolationError) as exc_info:
            resolve_gemma_route(WS_ID, preferred="invalid")

        assert exc_info.value.field == "preferred"

    def test_gemma_approved_models_contains_both_routes(self) -> None:
        """GEMMA_APPROVED_MODELS contains both free and paid routes."""
        assert GEMMA_FREE_MODEL in GEMMA_APPROVED_MODELS
        assert GEMMA_PAID_MODEL in GEMMA_APPROVED_MODELS
# ============================================================================
# 2. Workspace Budget Check Tests
# ============================================================================


class TestCheckWorkspaceModelBudget:
    """Test the fail-closed workspace budget gate (T-V3-009)."""

    def setup_method(self) -> None:
        reset_workspace_model_budget()
        reset_free_pool_state()

    def teardown_method(self) -> None:
        reset_workspace_model_budget()
        reset_free_pool_state()

    def test_free_model_always_allowed(self) -> None:
        """Free model requires no budget check."""
        result = check_workspace_model_budget(WS_ID, GEMMA_FREE_MODEL)
        assert result is True

    def test_paid_model_no_budget_raises(self) -> None:
        """Paid model with zero balance raises PolicyViolationError."""
        with pytest.raises(PolicyViolationError) as exc_info:
            check_workspace_model_budget(WS_ID, GEMMA_PAID_MODEL)

        assert exc_info.value.field == "workspace_budget"
        assert WS_ID in str(exc_info.value)

    def test_paid_model_with_budget_allowed(self) -> None:
        """Paid model with positive balance is allowed."""
        set_workspace_model_budget(WS_ID, 5.0)

        result = check_workspace_model_budget(WS_ID, GEMMA_PAID_MODEL)
        assert result is True

    def test_paid_model_with_zero_budget_raises(self) -> None:
        """Paid model with explicitly zero balance raises."""
        set_workspace_model_budget(WS_ID, 0.0)

        with pytest.raises(PolicyViolationError) as exc_info:
            check_workspace_model_budget(WS_ID, GEMMA_PAID_MODEL)

        assert exc_info.value.field == "workspace_budget"

    def test_non_gemma_model_raises(self) -> None:
        """Non-Gemma model is rejected with PolicyViolationError."""
        with pytest.raises(PolicyViolationError) as exc_info:
            check_workspace_model_budget(WS_ID, "gpt-4o-mini")

        assert exc_info.value.field == "model"
        assert "not in the approved Gemma routes" in str(exc_info.value)

    def test_budget_isolation_between_workspaces(self) -> None:
        """One workspace's budget doesn't cover another's paid call."""
        set_workspace_model_budget(WS_ID, 5.0)

        with pytest.raises(PolicyViolationError) as exc_info:
            check_workspace_model_budget(WS_OTHER, GEMMA_PAID_MODEL)
        assert exc_info.value.field == "workspace_budget"


# ============================================================================
# 3. Canonical Citations Tests
# ============================================================================


class TestCanonicalCitations:
    """Test that evidence records carry canonical provenance with source_hash."""

    def test_citation_with_source_hash_round_trips(self) -> None:
        """Citation with source_hash round-trips through model_dump/validate."""
        content = "The capital of France is Paris."
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

        citation = Citation(
            document_id="doc-1",
            chunk_id="chunk-1",
            quote=content[:100],
            source_hash=expected_hash,
        )
        record = EvidenceRecord(
            run_id="test-run",
            trace_id="test-trace",
            workspace_id=WS_ID,
            statement="Paris is the capital of France.",
            chunk_ids=["chunk-1"],
            citations=[citation],
            verdict="SUPPORTED",
        )

        dumped = record.model_dump()
        restored = EvidenceRecord.model_validate(dumped)
        assert restored.citations[0].source_hash == expected_hash
        assert restored.citations[0].chunk_id == "chunk-1"
    def test_evidence_record_requires_provenance(self) -> None:
        """EvidenceRecord without chunk_ids, citations, or chunk_id is rejected."""
        with pytest.raises(ValueError, match="provenance"):
            EvidenceRecord(
                run_id="test-run",
                trace_id="test-trace",
                statement="A claim without provenance",
            )

    def test_evidence_store_persists_source_hash(self) -> None:
        """InMemoryEvidenceStore persists source_hash on citations."""
        import asyncio

        async def _run():
            store = InMemoryEvidenceStore()
            citation = Citation(
                document_id="doc-1",
                chunk_id="chunk-1",
                quote="some content",
                source_hash="abc123def456",
            )
            record = EvidenceRecord(
                run_id="test-run",
                trace_id="test-trace",
                workspace_id=WS_ID,
                statement="A grounded claim",
                chunk_ids=["chunk-1"],
                citations=[citation],
                verdict="SUPPORTED",
                source_hash="abc123def456",
            )
            stored = await store.write(record)
            assert stored.citations[0].source_hash == "abc123def456"
            assert stored.source_hash == "abc123def456"

        asyncio.run(_run())


# ============================================================================
# 4. Restart / Cancellation Safety Tests
# ============================================================================


class TestRestartCancellationSafety:
    """Test that evidence records are idempotent (safe for restart/cancellation)."""

    def test_evidence_write_is_idempotent(self) -> None:
        """Writing the same record twice produces identical results."""
        import asyncio

        async def _run():
            store = InMemoryEvidenceStore()
            record = EvidenceRecord(
                run_id="test-run",
                trace_id="test-trace",
                workspace_id=WS_ID,
                statement="A claim",
                chunk_ids=["chunk-1"],
                citations=[
                    Citation(document_id="doc-1", chunk_id="chunk-1", quote="text")
                ],
                verdict="SUPPORTED",
            )

            stored1 = await store.write(record)
            stored2 = await store.write(record)

            assert stored1.id == stored2.id
            assert stored1.statement == stored2.statement
            assert stored1.verdict == stored2.verdict

            all_records = await store.list_for_run("test-run")
            assert len(all_records) == 1

        asyncio.run(_run())
    def test_evidence_store_rejects_unverified_records(self) -> None:
        """Evidence without provenance is rejected at construction time."""
        import asyncio

        async def _run():
            # EvidenceRecord's model_validator enforces provenance at construction
            with pytest.raises(ValueError, match="provenance"):
                EvidenceRecord(
                    run_id="test-run",
                    trace_id="test-trace",
                    statement="No provenance claim",
                )

        asyncio.run(_run())


# ============================================================================
# 5. StormPlanner Gemma Route Integration Tests
# ============================================================================


class _FakeStormLLM:
    """Minimal fake LLM for planner tests."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(self, request: Any) -> Any:
        from models.interfaces import GenerationResult

        self.calls.append(request.model)
        return GenerationResult(
            text=(
                '{"perspectives": ["general"], '
                '"questions": ["What is test?"], '
                '"outline": [{"title": "Test", "depth": 1}]}'
            ),
            model=request.model,
            prompt_tokens=10,
            completion_tokens=5,
        )

    async def stream(self, request: Any):
        yield ""


class TestStormPlannerGemmaRoute:
    """Test StormPlanner uses Gemma route resolution with workspace_id."""

    def setup_method(self) -> None:
        reset_workspace_model_budget()
        reset_free_pool_state()

    def teardown_method(self) -> None:
        reset_workspace_model_budget()
        reset_free_pool_state()

    def test_planner_uses_gemma_free_default(self) -> None:
        """StormPlanner without workspace_id uses Gemma free route."""
        planner = StormPlanner(llm=_FakeStormLLM())
        assert planner.model_name == GEMMA_FREE_MODEL

    def test_planner_paid_without_workspace_falls_back_to_free(self) -> None:
        """StormPlanner with paid model but no workspace_id falls back to free."""
        planner = StormPlanner(
            llm=_FakeStormLLM(),
            model_name=GEMMA_PAID_MODEL,
        )
        planner._check_gateway_policy()
        assert planner.model_name == GEMMA_FREE_MODEL

    def test_planner_arbitrary_paid_model_blocked(self) -> None:
        """Non-Gemma paid models are BLOCKED, never substituted."""
        planner = StormPlanner(
            llm=_FakeStormLLM(),
            model_name="gpt-4o-mini",
        )
        with pytest.raises(PlanError) as exc_info:
            planner._check_gateway_policy()

        assert exc_info.value.code == "BLOCKED"
        assert "paid model blocked" in str(exc_info.value)

    def test_planner_gemma_paid_with_workspace_budget_passes(self) -> None:
        """StormPlanner with Gemma paid + workspace budget passes the check."""
        set_workspace_model_budget(WS_ID, 5.0)

        planner = StormPlanner(
            llm=_FakeStormLLM(),
            model_name=GEMMA_PAID_MODEL,
            workspace_id=WS_ID,
        )
        planner._check_gateway_policy()
        assert planner.model_name == GEMMA_PAID_MODEL

    def test_planner_gemma_paid_no_budget_raises_policy_violation(self) -> None:
        """StormPlanner with Gemma paid but no workspace budget raises."""
        planner = StormPlanner(
            llm=_FakeStormLLM(),
            model_name=GEMMA_PAID_MODEL,
            workspace_id=WS_ID,
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            planner._check_gateway_policy()

        assert exc_info.value.field == "workspace_budget"

    def test_free_models_contains_gemma_routes(self) -> None:
        """FREE_MODELS includes both Gemma free and paid routes."""
        assert GEMMA_FREE_MODEL in FREE_MODELS
        assert GEMMA_PAID_MODEL in FREE_MODELS
