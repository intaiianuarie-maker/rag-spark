"""Contract checks for the V3 research evidence contracts (card A0).

Pure validation — no IO, no services. These checks pin the shared seams that
the persistent evidence adapter (R0) and the STORM-backed planner (R1)
implement:

- :class:`ResearchEvidence` is scoped to one research run and carries
  original-source provenance (document version/chunk, quote span, source hash)
  plus an explicit verification state. A caller-supplied claim is ``UNVERIFIED``.
- :class:`ReportClaim` ties a statement to evidence ids and original-source
  citations; ``SUPPORTED`` requires verified evidence.
"""

from __future__ import annotations

from .interfaces import (
    Citation,
    ClaimStatus,
    EvidenceVerificationState,
    ReportClaim,
    ResearchEvidence,
)


def _fail(message: str) -> None:
    raise AssertionError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


def _require_instance(value: object, expected_type: type, what: str) -> None:
    if not isinstance(value, expected_type):
        _fail(f"{what}: expected {expected_type.__name__}, got {type(value).__name__}")


def check_research_evidence_contract(evidence: ResearchEvidence) -> None:
    """Evidence is run-scoped, source-pinned and explicitly verified.

    The check validates the *supplied* evidence (rew1 B10).
    """
    _require_instance(evidence, ResearchEvidence, "research evidence")
    _require(bool(evidence.workspace_id), "workspace_id must be non-empty")
    _require(bool(evidence.claim), "claim must be non-empty")
    _require(bool(evidence.quote), "quote must be non-empty")
    _require_instance(evidence.verification, EvidenceVerificationState, "verification state")

    # Original-source provenance: quote span must be a valid range when present.
    if evidence.quote_start is not None and evidence.quote_end is not None:
        _require(
            evidence.quote_start >= 0 and evidence.quote_end >= evidence.quote_start,
            "quote span must be a valid non-negative range",
        )
    _require(isinstance(evidence.chunk_ids, list), "chunk_ids must be a list (never None)")

    # Non-UNVERIFIED evidence must carry real provenance.
    if evidence.verification is not EvidenceVerificationState.UNVERIFIED:
        _require(
            bool(evidence.document_version) and bool(evidence.chunk_id),
            "verified evidence must reference a document version and chunk",
        )


def check_report_claim_contract(claim: ReportClaim) -> None:
    """A claim is only SUPPORTED with verified evidence and real citations.

    The check validates the *supplied* claim (rew1 B10).
    """
    _require_instance(claim, ReportClaim, "report claim")
    _require(bool(claim.statement), "statement must be non-empty")
    _require_instance(claim.status, ClaimStatus, "claim status")
    if claim.status is ClaimStatus.SUPPORTED:
        _require(
            len(claim.evidence_ids) >= 1,
            "SUPPORTED claims must reference at least one evidence id",
        )
    for citation in claim.citations:
        _require_instance(citation, Citation, "citation")
        _require(bool(citation.chunk_id), "citations must resolve to a real chunk")


def check_evidence_run_isolation_contract(
    run_one: list[ResearchEvidence], run_two: list[ResearchEvidence]
) -> None:
    """Two runs in one workspace cannot mix evidence.

    Validates the supplied evidence collections against their run ids
    (rew1 B10); previously this only intersected two lists of strings.
    """
    _require_instance(run_one, list, "run_one")
    _require_instance(run_two, list, "run_two")
    ids_one: set[str] = set()
    for item in run_one:
        _require_instance(item, ResearchEvidence, "run_one item")
        _require(bool(item.run_id), "evidence must carry a run_id")
        ids_one.add(item.id)
    ids_two: set[str] = set()
    for item in run_two:
        _require_instance(item, ResearchEvidence, "run_two item")
        _require(bool(item.run_id), "evidence must carry a run_id")
        ids_two.add(item.id)
    overlap = ids_one & ids_two
    _require(not overlap, f"evidence ids must not be shared across runs: {sorted(overlap)}")
    runs_one = {item.run_id for item in run_one}
    _require(len(runs_one) <= 1, "run_one must contain evidence from a single research run")
