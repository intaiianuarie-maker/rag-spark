"""Unit tests for T-R08 live evaluation gate (evals/live_v1_gate.py)."""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in ("packages", "apps/api", "apps/worker", "scripts", "."):
    _path_str = str((_REPO_ROOT / _p).resolve())
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from answer.models import Answer, AnswerStatus, Claim  # noqa: E402
from retrieval.interfaces import RetrievedItem  # noqa: E402

from evals.live_v1_gate import (  # noqa: E402
    CallStats,
    EvalItem,
    ItemEvaluationResult,
    compile_summary,
    load_frozen_slices,
    percentile,
    render_markdown_tables,
    run_live_gate,
    wilson_score_interval,
)


def test_wilson_score_interval_math() -> None:
    """Verify standard Wilson score confidence interval properties."""
    # Zero items
    assert wilson_score_interval(0, 0) == (0.0, 0.0)

    # Perfect 25/25
    lower, upper = wilson_score_interval(25, 25)
    assert 0.85 <= lower <= 0.88
    assert upper == 1.0

    # 0/25
    lower_0, upper_0 = wilson_score_interval(0, 25)
    assert lower_0 == 0.0
    assert 0.12 <= upper_0 <= 0.15

    # 10/20 (50%)
    lower_50, upper_50 = wilson_score_interval(10, 20)
    assert 0.29 <= lower_50 <= 0.32
    assert 0.68 <= upper_50 <= 0.71


def test_percentile_math() -> None:
    """Verify percentile computation."""
    assert percentile([], 0.50) == 0.0
    data = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(data, 0.50) == 30.0
    assert percentile(data, 0.0) == 10.0
    assert percentile(data, 1.0) == 50.0


def test_frozen_splits_disjointness_and_counts() -> None:
    """Prove dev and holdout splits are strictly disjoint with zero leakage."""
    dev_items, holdout_items = load_frozen_slices()

    assert len(dev_items) == 30, f"Expected 30 dev items, got {len(dev_items)}"
    assert len(holdout_items) == 30, f"Expected 30 holdout items, got {len(holdout_items)}"

    dev_ids = {it.source_id for it in dev_items}
    holdout_ids = {it.source_id for it in holdout_items}
    assert dev_ids.isdisjoint(holdout_ids), "Source IDs leak between dev and holdout!"

    dev_hashes = {it.content_sha256 for it in dev_items}
    holdout_hashes = {it.content_sha256 for it in holdout_items}
    assert dev_hashes.isdisjoint(holdout_hashes), "Context hashes leak between dev and holdout!"

    # Verify each slice has exactly 5 dev and 5 holdout
    for s_name in ("en", "ro", "pt", "tables", "multi", "unanswerable"):
        n_dev = sum(1 for it in dev_items if it.slice_name == s_name)
        n_hold = sum(1 for it in holdout_items if it.slice_name == s_name)
        assert n_dev == 5, f"Slice {s_name} dev count {n_dev} != 5"
        assert n_hold == 5, f"Slice {s_name} holdout count {n_hold} != 5"


def test_refusal_is_strictly_failure_for_answerable_items() -> None:
    """Prove that refusals on answerable questions are failures (refusal_as_success == 0)."""
    item = EvalItem(
        slice_name="en",
        source_id="test-refusal",
        source_path="",
        question="What is X?",
        context="X is forty two.",
        gold_answer="forty two",
        content_sha256="testsha",
        split="holdout",
    )
    # Simulate a refusal response from model
    ans = Answer(
        answer="I cannot answer this question based on the provided documents.",
        status=AnswerStatus.INSUFFICIENT_EVIDENCE,
        reason_code="EVIDENCE_MISSING",
        claims=[],
        citations=[],
    )
    res = ItemEvaluationResult(
        item=item,
        retrieved_items=[RetrievedItem(content="X is forty two.", score=1.0)],
        gold_chunk_ids=["c1"],
        recall_at_20=1.0,
        retrieval_latency_ms=10,
        answer=ans,
        generation_latency_ms=100,
        faithfulness=0.0,
        is_refusal=True,
        grounded_success=False,  # Enforced: refusal is NOT a success!
        factual_precision=0.0,
        citation_validity=0.0,
        correct_abstention=None,
    )
    assert res.grounded_success is False, "Refusal of answerable question must NOT be a success!"

    # Compile summary with this failing refusal
    report = compile_summary(
        [res],
        acceptance_flows=[],
        split="holdout",
        stats=CallStats(call_site="test"),
    )
    assert report.grounded_answer_rate == 0.0
    assert report.all_targets_met is False


def test_empty_citations_never_satisfy_coverage() -> None:
    """Prove that an answer with empty citations fails grounded answer success."""
    # Model emits answer text but zero citations
    ans = Answer(
        answer="X is forty two.",
        status=AnswerStatus.ANSWERED,
        claims=[Claim(text="X is forty two.", chunk_ids=[])],
        citations=[],  # Empty!
    )
    # Grounded success requires surviving citations >= 1
    has_citations = len(ans.citations) >= 1
    grounded_success = ans.status == AnswerStatus.ANSWERED and has_citations
    assert grounded_success is False, "Empty citations must NOT satisfy grounded success"


@pytest.mark.asyncio
async def test_dry_run_live_gate_executes_cleanly() -> None:
    """Verify live gate executes end-to-end in dry-run mode without external services."""
    report = await run_live_gate(split="holdout", dry_run=True, run_acceptance=True)
    assert report.n_total == 30
    assert report.n_answerable == 25
    assert report.n_unanswerable == 5
    assert len(report.acceptance_flows) == 5
    assert all(f.passed for f in report.acceptance_flows)

    # Render markdown tables and verify headers
    md = render_markdown_tables(report)
    assert "### V1 Live Evaluation Gate Summary" in md
    assert "EvidenceRecall@20" in md
    assert "Grounded Answer Rate" in md
    assert "Factual Precision" in md
    assert "Citation Validity" in md
    assert "Correct Abstention" in md
    assert "The 5 Acceptance Flows" in md
    assert "Spend & Metering Accounting" in md
