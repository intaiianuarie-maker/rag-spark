"""Evaluation package (TASK-M8-001) — dataset/experiment/run + RAGAS-style metrics.

Public surface:
    GoldenExample / EvaluationDataset / EvaluationRun  — models
    compute_recall / compute_precision / compute_mrr / compute_ndcg
    compute_citation_accuracy / score_faithfulness     — metrics
    evaluate_dataset                                    — runner
    build_comparison_report / ComparisonReport          — report
"""

from .compare import (
    ABComparison,
    MetricDelta,
    RunRecord,
    build_ab_report,
    compare_runs,
    render_comparison_text,
)
from .metrics import (
    compute_citation_accuracy,
    compute_mrr,
    compute_ndcg,
    compute_precision,
    compute_recall,
    score_faithfulness,
)
from .models import EvaluationDataset, EvaluationRun, GoldenExample
from .report import ComparisonReport, MetricRow, RunSummary, build_comparison_report
from .runner import RetrievalResult, evaluate_dataset

__all__ = [
    "ABComparison",
    "ComparisonReport",
    "EvaluationDataset",
    "EvaluationRun",
    "GoldenExample",
    "MetricDelta",
    "MetricRow",
    "RetrievalResult",
    "RunRecord",
    "RunSummary",
    "build_ab_report",
    "build_comparison_report",
    "compare_runs",
    "compute_citation_accuracy",
    "compute_mrr",
    "compute_ndcg",
    "compute_precision",
    "compute_recall",
    "evaluate_dataset",
    "render_comparison_text",
    "score_faithfulness",
]
