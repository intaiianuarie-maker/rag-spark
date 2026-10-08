"""Unit tests for the eval harness (TASK-M8-001).

Covers ranking metrics (recall/precision/MRR/NDCG), citation accuracy,
faithfulness (default proxy + injected scorer + clamping), the runner's
per-metric means, and the comparison report's winner selection.
"""

from __future__ import annotations

import pytest
from app.eval import (
    ComparisonReport,
    EvaluationDataset,
    GoldenExample,
    MetricRow,
    RetrievalResult,
    build_comparison_report,
    compute_citation_accuracy,
    compute_mrr,
    compute_ndcg,
    compute_precision,
    compute_recall,
    evaluate_dataset,
    score_faithfulness,
)

# ---- ranking metrics ---------------------------------------------------------


def test_recall_perfect_partial_and_empty():
    assert compute_recall(["a", "b", "c"], ["a", "b"]) == 1.0
    assert compute_recall(["a", "x"], ["a", "b"]) == 0.5
    assert compute_recall([], ["a"]) == 0.0
    assert compute_recall(["a"], []) == 0.0  # no ground truth -> 0


def test_precision_penalizes_noise():
    assert compute_precision(["a", "b"], ["a", "b", "c"]) == 1.0
    assert compute_precision(["a", "x", "y"], ["a"]) == 1 / 3
    assert compute_precision([], ["a"]) == 0.0


def test_mrr_rank_aware():
    assert compute_mrr(["x", "a", "b"], ["a"]) == 0.5
    assert compute_mrr(["a", "b"], ["a"]) == 1.0
    assert compute_mrr(["x", "y"], ["a"]) == 0.0
    # duplicates collapsed: first occurrence wins
    assert compute_mrr(["a", "a", "b"], ["b"]) == 1 / 3


def test_ndcg_ideal_vs_shuffled():
    assert compute_ndcg(["a", "b", "c"], ["a", "b"]) == 1.0
    # relevant docs ranked last still partial credit > 0, < 1
    partial = compute_ndcg(["x", "y", "a"], ["a"])
    assert 0.0 < partial < 1.0
    assert compute_ndcg(["x", "y"], ["a"]) == 0.0
    assert compute_ndcg(["a"], []) == 0.0


def test_citation_accuracy_penalizes_fabrication():
    assert compute_citation_accuracy(["a", "b"], ["a", "b", "c"]) == 1.0
    # one fabricated citation halves the score
    assert compute_citation_accuracy(["a", "fake"], ["a"]) == 0.5
    assert compute_citation_accuracy([], ["a"]) == 0.0  # nothing cited -> 0


# ---- faithfulness ------------------------------------------------------------


def test_faithfulness_proxy_full_and_partial_overlap():
    assert score_faithfulness("the cat sat", "the cat sat on the mat") == 1.0
    partial = score_faithfulness("the cat slept", "the cat sat on the mat")
    assert 0.0 < partial < 1.0
    assert score_faithfulness("", "context") == 0.0


def test_faithfulness_injected_scorer_is_used():
    assert score_faithfulness("a", "b", scorer=lambda _a, _c: 0.42) == 0.42


def test_faithfulness_clamps_out_of_range_scorer():
    assert score_faithfulness("a", "b", scorer=lambda _a, _c: 5.0) == 1.0
    assert score_faithfulness("a", "b", scorer=lambda _a, _c: -1.0) == 0.0


# ---- runner ------------------------------------------------------------------


def _dataset() -> EvaluationDataset:
    return EvaluationDataset(
        id="ds-1",
        name="golden",
        examples=[
            GoldenExample(question="q1", expected_sources=["a"]),
            GoldenExample(question="q2", expected_sources=["b", "c"]),
        ],
    )


@pytest.mark.asyncio
async def test_evaluate_dataset_means_over_examples():
    def retrieve(q: str) -> RetrievalResult:
        # q1 retrieves a perfectly; q2 retrieves b + noise (recall 0.5)
        return RetrievalResult(doc_ids=["a"] if q == "q1" else ["b", "x"])

    run = await evaluate_dataset(_dataset(), retrieve, run_id="r1")
    assert run.id == "r1"
    assert run.results["recall"] == (1.0 + 0.5) / 2
    assert run.results["precision"] == (1.0 + 0.5) / 2
    assert run.config_snapshot["n_questions"] == 2


@pytest.mark.asyncio
async def test_evaluate_dataset_accepts_async_retriever():
    async def aretrieve(q: str) -> RetrievalResult:
        return RetrievalResult(doc_ids=["a"] if q == "q1" else ["b", "x"])

    run = await evaluate_dataset(_dataset(), aretrieve, run_id="r-async")
    assert run.results["recall"] == (1.0 + 0.5) / 2


@pytest.mark.asyncio
async def test_evaluate_dataset_empty_is_zeroed_not_error():
    empty = EvaluationDataset(id="ds-empty", name="empty", examples=[])
    run = await evaluate_dataset(empty, lambda _q: RetrievalResult(doc_ids=[]))
    assert run.results["recall"] == 0.0
    assert run.config_snapshot["n_questions"] == 0


# ---- comparison report -------------------------------------------------------


def test_build_comparison_report_picks_winners():
    rows = [
        MetricRow(question="q", run_id="baseline", recall=0.2, ndcg=0.9),
        MetricRow(question="q", run_id="candidate", recall=0.8, ndcg=0.5),
    ]
    report = build_comparison_report(rows)
    assert isinstance(report, ComparisonReport)
    assert {s.run_id for s in report.runs} == {"baseline", "candidate"}
    assert report.winners["recall"] == "candidate"
    assert report.winners["ndcg"] == "baseline"
