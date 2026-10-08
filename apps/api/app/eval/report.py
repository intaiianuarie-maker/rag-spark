"""Eval report — comparison report across experiment runs (TASK-M8-001).

Aggregates per-question metric rows into a comparison report: per-metric means,
a winner per metric, and a stable, human-readable ordering. Pure + testable.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Canonical metric keys reported by the harness, in report order.
REPORT_METRICS = (
    "recall",
    "precision",
    "mrr",
    "ndcg",
    "citation_accuracy",
    "faithfulness",
)


class MetricRow(BaseModel):
    """One question's metrics, plus the ids needed to trace a row."""

    question: str
    run_id: str
    recall: float = 0.0
    precision: float = 0.0
    mrr: float = 0.0
    ndcg: float = 0.0
    citation_accuracy: float = 0.0
    faithfulness: float = 0.0


class RunSummary(BaseModel):
    run_id: str
    n_questions: int = 0
    means: dict[str, float] = Field(default_factory=dict)


class ComparisonReport(BaseModel):
    runs: list[RunSummary] = Field(default_factory=list)
    #: metric -> run_id of the best mean (ties broken by first-listed run).
    winners: dict[str, str] = Field(default_factory=dict)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize_run(run_id: str, rows: list[MetricRow]) -> RunSummary:
    """Aggregate one run's rows into per-metric means."""
    run_rows = [r for r in rows if r.run_id == run_id]
    means = {
        metric: _mean([float(getattr(r, metric)) for r in run_rows])
        for metric in REPORT_METRICS
    }
    return RunSummary(run_id=run_id, n_questions=len(run_rows), means=means)


def build_comparison_report(rows: list[MetricRow]) -> ComparisonReport:
    """Summarize every run present in ``rows`` and pick a winner per metric.

    Higher-is-better for every metric. Ties keep the first-listed run (stable
    given a deterministic row order).
    """
    run_ids: list[str] = []
    for row in rows:
        if row.run_id not in run_ids:
            run_ids.append(row.run_id)

    summaries = [summarize_run(rid, rows) for rid in run_ids]

    winners: dict[str, str] = {}
    for metric in REPORT_METRICS:
        best_run = ""
        best_value = float("-inf")
        for summary in summaries:
            value = summary.means.get(metric, 0.0)
            if value > best_value:
                best_value = value
                best_run = summary.run_id
        if best_run:
            winners[metric] = best_run

    return ComparisonReport(runs=summaries, winners=winners)
