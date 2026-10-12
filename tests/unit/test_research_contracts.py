"""V3 (A0) research contract tests — evidence provenance and report claims.

Unit/contract only: no services, no vendor SDKs. Locks the seams the
persistent evidence adapter (R0) and STORM-backed planner (R1) implement, and
proves the JSON fixture examples validate against the typed contracts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from research import (
    Citation,
    ClaimStatus,
    EvidenceVerificationState,
    ReportClaim,
    ResearchEvidence,
    ResearchReport,
    ResearchRun,
    ResearchStatus,
)
from research.contract_tests import (
    check_evidence_run_isolation_contract,
    check_report_claim_contract,
    check_research_evidence_contract,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "research"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _verified_evidence() -> ResearchEvidence:
    return ResearchEvidence(
        id="ev-0001",
        workspace_id="ws-0001",
        query_id="q-0001",
        claim="PostgreSQL handles multi-tenant indexing.",
        source="doc-pg",
        quote="PostgreSQL provides schema-level multi-tenancy.",
        run_id="rrun-0001",
        subquestion_id="sq-0001",
        document_id="doc-pg",
        document_version="v1",
        chunk_id="chunk-pg-1",
        quote_start=12,
        quote_end=58,
        source_hash="sha256:9f2c1a4b7e0d3f5a6c8b1d4e7f0a2c5b8d1e4f7a0c3b6d9e2f5a8c1b4d7e0f3a",
        verification=EvidenceVerificationState.VERIFIED,
        verification_reason=(
            "Quote span resolves to stored chunk chunk-pg-1 in document version v1."
        ),
        chunk_ids=["chunk-pg-1"],
    )


def test_evidence_fixture_validates() -> None:
    evidence = ResearchEvidence.model_validate(_load("evidence.example.json"))
    check_research_evidence_contract(evidence)
    assert evidence.verification is EvidenceVerificationState.VERIFIED
    assert evidence.run_id == "rrun-0001"
    assert evidence.quote_start == 12 and evidence.quote_end == 58


def test_caller_supplied_claim_is_never_verified() -> None:
    caller = ResearchEvidence(
        id="ev-caller",
        workspace_id="ws-1",
        claim="Supplied by the caller",
        source="model-output",
        quote="some span",
    )
    check_research_evidence_contract(caller)
    assert caller.verification is EvidenceVerificationState.UNVERIFIED


def test_evidence_quote_span_must_be_valid_range() -> None:
    with pytest.raises(ValueError):
        ResearchEvidence(
            id="ev-bad",
            workspace_id="ws-1",
            claim="x",
            source="s",
            quote="q",
            quote_start=50,
            quote_end=10,
        )


def test_report_claim_fixture_validates() -> None:
    claim = ReportClaim.model_validate(_load("report_claim.example.json"))
    check_report_claim_contract(claim)
    assert claim.status is ClaimStatus.SUPPORTED
    assert claim.evidence_ids == ["ev-0001"]
    assert claim.citations[0].chunk_id == "chunk-pg-1"


def test_supported_claim_requires_evidence_ids() -> None:
    """rew1 B8: SUPPORTED with no evidence is now schema-rejected."""
    with pytest.raises(ValidationError):
        ReportClaim(statement="No evidence", status=ClaimStatus.SUPPORTED)
    # The contract helper still rejects an insufficiently supported claim.
    with pytest.raises(AssertionError):
        check_report_claim_contract(
            ReportClaim.model_construct(
                statement="No evidence", status=ClaimStatus.SUPPORTED, evidence_ids=[]
            )
        )


def test_unknown_claim_never_cites_fabricated_chunks() -> None:
    """rew1 B8: a citation must resolve to a real chunk at construction."""
    with pytest.raises(ValidationError):
        ReportClaim(
            statement="Unsupported",
            status=ClaimStatus.UNKNOWN,
            citations=[Citation(document_id="doc-x", chunk_id="")],
        )


def test_evidence_run_isolation() -> None:
    ev_a = ResearchEvidence(
        id="ev-1", workspace_id="ws-1", run_id="run-A", claim="a", source="s", quote="q"
    )
    ev_b = ResearchEvidence(
        id="ev-2", workspace_id="ws-1", run_id="run-A", claim="b", source="s", quote="q"
    )
    ev_c = ResearchEvidence(
        id="ev-3", workspace_id="ws-1", run_id="run-B", claim="c", source="s", quote="q"
    )
    check_evidence_run_isolation_contract([ev_a, ev_b], [ev_c])
    with pytest.raises(AssertionError):
        check_evidence_run_isolation_contract([ev_a, ev_b], [ev_b, ev_c])


def test_research_run_scopes_investigation() -> None:
    run = ResearchRun(run_id="rrun-0001", workspace_id="ws-0001", topic="Consensus protocols")
    assert run.status is ResearchStatus.PLANNED
    assert run.workspace_id == "ws-0001"


def test_report_abstains_without_verified_evidence() -> None:
    report = ResearchReport(
        id="rep-0001",
        workspace_id="ws-0001",
        topic="Empty topic",
        status=ResearchStatus.FAILED,
        claims=[
            ReportClaim(
                statement="Unverified",
                evidence_ids=[],
                status=ClaimStatus.UNKNOWN,
            )
        ],
    )
    # No SUPPORTED claims -> the report abstains rather than claiming success.
    assert all(c.status is not ClaimStatus.SUPPORTED for c in report.claims)
    assert report.supported_findings == []
    assert report.disputed_candidates == []


def test_report_separates_disputed_candidates() -> None:
    report = ResearchReport(
        id="rep-0002",
        workspace_id="ws-1",
        topic="t",
        disputed_candidates=["Storage cluster cannot perform automated daily backups."],
        claims=[
            ReportClaim(
                statement="Backups are automated.",
                evidence_ids=["ev-a"],
                status=ClaimStatus.DISPUTED,
            )
        ],
    )
    assert report.disputed_candidates
    assert report.claims[0].status is ClaimStatus.DISPUTED
