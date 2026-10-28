"""Unit tests for M8-001b A/B comparison reports."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")

from app.eval import (
    EvaluationRun,
    RunSummary,
    compare_runs,
    render_comparison_text,
)

pytestmark = pytest.mark.unit


def test_delta_direction_and_cost_latency() -> None:
    baseline = RunSummary(
        run_id="base",
        n_questions=2,
        means={"recall": 0.5, "ndcg": 0.4},
        cost=1.0,
        latency_ms=100.0,
        tokens=50,
    )
    candidate = RunSummary(
        run_id="cand",
        n_questions=2,
        means={"recall": 0.8, "ndcg": 0.3},
        cost=0.8,
        latency_ms=120.0,
        tokens=40,
    )
    cmp = compare_runs(baseline, candidate, dimension="reranker")
    assert cmp.dimension == "reranker"
    by_metric = {d.metric: d for d in cmp.metric_deltas}
    assert by_metric["recall"].delta == pytest.approx(0.3)
    assert by_metric["recall"].direction == "improved"
    assert by_metric["ndcg"].direction == "regressed"
    assert cmp.cost_delta == pytest.approx(-0.2)
    assert cmp.cost_direction == "improved"  # lower cost is better
    assert cmp.latency_delta_ms == pytest.approx(20.0)
    assert cmp.latency_direction == "regressed"
    assert cmp.tokens_delta == -10
    text = render_comparison_text(cmp)
    assert "base vs cand" in text


def test_evaluation_run_normalization() -> None:
    a = EvaluationRun(
        id="a", dataset_id="d", results={"recall": 0.1}, cost=2.0, latency_ms=10.0, tokens=5
    )
    b = EvaluationRun(
        id="b", dataset_id="d", results={"recall": 0.2}, cost=1.0, latency_ms=5.0, tokens=3
    )
    cmp = compare_runs(a, b)
    assert cmp.baseline_run_id == "a"
    assert cmp.candidate_run_id == "b"
    by_metric = {d.metric: d for d in cmp.metric_deltas}
    assert by_metric["recall"].delta == pytest.approx(0.1)


@pytest.mark.parametrize("dimension", ["embedder", "chunker", "retriever", "model", "prompt"])
def test_external_pipeline_dimension_validated(dimension):
    snapshot = {
        "dataset_sha256": "fixture-sha",
        "n_questions": 2,
        "scorer": "lexical_overlap_v1",
        "sensitive": False,
        "accounting": "imported_trace",
        "retrieval": {"top_k": 10, "mode": "hybrid", "reranker": True},
        "experiment": {dimension: "a"},
    }
    a = EvaluationRun(id="a", dataset_id="fixed", config_snapshot=snapshot)
    b = EvaluationRun(
        id="b",
        dataset_id="fixed",
        config_snapshot={
            **snapshot,
            "experiment": {dimension: "b"},
        },
    )
    assert compare_runs(a, b, dimension=dimension).configuration_validated
    b.config_snapshot["retrieval"] = {**snapshot["retrieval"], "top_k": 20}
    with pytest.raises(ValueError, match="only in the selected dimension"):
        compare_runs(a, b, dimension=dimension)
