"""Evaluation package (TASK-M8-001) — dataset/experiment/run + RAGAS-style metrics.

Public surface:
    GoldenExample / EvaluationDataset / EvaluationRun  — models
    compute_recall / compute_precision / compute_mrr / compute_ndcg
    compute_citation_accuracy / score_faithfulness     — metrics
    evaluate_dataset                                    — runner
    build_comparison_report / ComparisonReport          — report
"""

from .metrics import (
    compute_citation_accuracy,
    compute_mrr,
    compute_ndcg,
    compute_precision,
    compute_recall,
    score_faithfulness,
)
from .models import EvaluationDataset, EvaluationRun, GoldenExample
from .report import ComparisonReport, MetricRow, build_comparison_report
from .runner import RetrievalResult, evaluate_dataset

__all__ = [
    "ComparisonReport",
    "EvaluationDataset",
    "EvaluationRun",
    "GoldenExample",
    "MetricRow",
    "RetrievalResult",
    "build_comparison_report",
    "compute_citation_accuracy",
    "compute_mrr",
    "compute_ndcg",
    "compute_precision",
    "compute_recall",
    "evaluate_dataset",
    "score_faithfulness",
]
