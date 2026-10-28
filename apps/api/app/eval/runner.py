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

import hashlib
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

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
        latency_ms: float = 0.0,
        cost: float = 0.0,
        tokens: int = 0,
        accounting_complete: bool = False,
        replay_faithfulness: float | None = None,
    ) -> None:
        self.doc_ids = doc_ids
        self.context = context
        self.cited_doc_ids = cited_doc_ids if cited_doc_ids is not None else doc_ids
        self.answer = answer
        self.latency_ms = latency_ms
        self.cost = cost
        self.tokens = tokens
        self.accounting_complete = accounting_complete
        self.replay_faithfulness = replay_faithfulness


#: retrieve(question) -> RetrievalResult (sync or async)
RetrieverFn = Callable[[str], RetrievalResult | Awaitable[RetrievalResult]]


async def evaluate_dataset(
    dataset: EvaluationDataset,
    retrieve: RetrieverFn,
    run_id: str | None = None,
    faithfulness_scorer: FaithfulnessScorer | None = None,
    config_snapshot: dict[str, Any] | None = None,
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
    total_cost = 0.0
    total_latency = 0.0
    total_tokens = 0
    accounting_complete = bool(dataset.examples)
    replay_inputs: list[dict[str, Any]] = []

    for example in dataset.examples:
        outcome = retrieve(example.question)
        result: RetrievalResult = await outcome if inspect.isawaitable(outcome) else outcome
        total_cost += float(result.cost)
        total_latency += float(result.latency_ms)
        total_tokens += int(result.tokens)
        accounting_complete = accounting_complete and result.accounting_complete
        gt = example.expected_sources
        faithfulness = (
            result.replay_faithfulness
            if result.replay_faithfulness is not None
            else score_faithfulness(result.answer, result.context, faithfulness_scorer)
        )
        # IDs and numeric measurements are sufficient to reproduce scoring;
        # keep document text and generated answers out of durable storage.
        replay_inputs.append(
            {
                "doc_ids": list(result.doc_ids),
                "cited_doc_ids": list(result.cited_doc_ids),
                "cost": result.cost,
                "latency_ms": result.latency_ms,
                "tokens": result.tokens,
                "accounting_complete": result.accounting_complete,
                "replay_faithfulness": faithfulness,
            }
        )
        rows.append(
            MetricRow(
                question=example.question,
                run_id=run_id,
                cost=result.cost,
                latency_ms=result.latency_ms,
                tokens=result.tokens,
                recall=compute_recall(result.doc_ids, gt),
                precision=compute_precision(result.doc_ids, gt),
                mrr=compute_mrr(result.doc_ids, gt),
                ndcg=compute_ndcg(result.doc_ids, gt),
                citation_accuracy=compute_citation_accuracy(result.cited_doc_ids, gt),
                faithfulness=faithfulness,
            )
        )

    means = {
        metric: (sum(getattr(r, metric) for r in rows) / len(rows) if rows else 0.0)
        for metric in REPORT_METRICS
    }
    return EvaluationRun(
        id=run_id,
        dataset_id=dataset.id,
        config_snapshot={
            **(config_snapshot or {}),
            "n_questions": len(dataset.examples),
            "dataset": dataset.model_dump(mode="json"),
            "dataset_sha256": hashlib.sha256(
                json.dumps(
                    [e.model_dump(mode="json") for e in dataset.examples],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest(),
            "scorer": "lexical_overlap_v1" if faithfulness_scorer is None else "injected",
            "replay_inputs": replay_inputs,
        },
        results=means,
        cost=total_cost,
        latency_ms=total_latency,
        tokens=total_tokens,
        accounting_complete=accounting_complete,
    )


async def replay_snapshot(snapshot: dict[str, Any]) -> EvaluationRun:
    """Recompute metrics/accounting from a saved snapshot without external I/O.

    Faithfulness is a recorded scorer output (not a fresh judge invocation).
    All ranking metrics are recomputed from the recorded ID lists.
    """
    dataset = EvaluationDataset.model_validate(snapshot["dataset"])
    inputs = snapshot["replay_inputs"]
    if not isinstance(inputs, list) or len(inputs) != len(dataset.examples):
        raise ValueError("snapshot replay inputs do not match the dataset")
    records = iter(inputs)
    result = await evaluate_dataset(
        dataset,
        lambda _: RetrievalResult(**next(records)),
        config_snapshot=snapshot,
    )
    result.config_snapshot["scorer"] = snapshot["scorer"]
    return result
