"""Eval runner — drive a dataset through retrieval + metrics (TASK-M8-001).

The runner is retrieval-agnostic: callers inject a ``retrieve`` callable that
maps a question to its retrieved document ids (plus optional context/citations/
answer). This keeps the harness hermetic in CI (fakes) and reusable against the
real orchestrator at runtime. Faithfulness uses an injectable scorer (see
``metrics.score_faithfulness``).

``retrieve`` may be sync or async; the runner awaits it when it returns an
awaitable, so fakes stay simple while the real orchestrator retriever (async)
plugs in unchanged.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable

from .metrics import FaithfulnessScorer, score_faithfulness
from .models import EvaluationDataset, EvaluationRun
from .report import REPORT_METRICS, MetricRow


class RetrievalResult:
    """What the injected retriever returns for one question."""

    def __init__(
        self,
        doc_ids: list[str],
        context: str = "",
        cited_doc_ids: list[str] | None = None,
        answer: str = "",
    ) -> None:
        self.doc_ids = doc_ids
        self.context = context
        self.cited_doc_ids = cited_doc_ids if cited_doc_ids is not None else doc_ids
        self.answer = answer


#: retrieve(question) -> RetrievalResult (sync or async)
RetrieverFn = Callable[[str], RetrievalResult | Awaitable[RetrievalResult]]


async def evaluate_dataset(
    dataset: EvaluationDataset,
    retrieve: RetrieverFn,
    run_id: str | None = None,
    faithfulness_scorer: FaithfulnessScorer | None = None,
) -> EvaluationRun:
    """Run every golden example and collect a per-metric mean run."""
    from .metrics import (
        compute_citation_accuracy,
        compute_mrr,
        compute_ndcg,
        compute_precision,
        compute_recall,
    )

    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    rows: list[MetricRow] = []

    for example in dataset.examples:
        outcome = retrieve(example.question)
        result: RetrievalResult = (
            await outcome if inspect.isawaitable(outcome) else outcome
        )
        gt = example.expected_sources
        rows.append(
            MetricRow(
                question=example.question,
                run_id=run_id,
                recall=compute_recall(result.doc_ids, gt),
                precision=compute_precision(result.doc_ids, gt),
                mrr=compute_mrr(result.doc_ids, gt),
                ndcg=compute_ndcg(result.doc_ids, gt),
                citation_accuracy=compute_citation_accuracy(result.cited_doc_ids, gt),
                faithfulness=score_faithfulness(
                    result.answer, result.context, faithfulness_scorer
                ),
            )
        )

    means = {
        metric: (sum(getattr(r, metric) for r in rows) / len(rows) if rows else 0.0)
        for metric in REPORT_METRICS
    }
    return EvaluationRun(
        id=run_id,
        dataset_id=dataset.id,
        config_snapshot={"n_questions": len(dataset.examples)},
        results=means,
    )
