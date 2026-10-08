"""S0 eval-harness smoke driver (test_manager). NOT product code.

Loads evals/golden_smoke.jsonl, runs it through the app.eval harness against a
deterministic FAKE retriever (perfect retrieval; one fabricated citation on the
multi-doc row), and prints the per-metric means + a comparison report. Proves
the harness end-to-end with zero external services (CI-hermetic).

Run (from repo root):  /root/ko-venv/bin/python evals/smoke_eval.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# The pyproject `pythonpath` only applies under pytest; add it here so the
# driver runs standalone. Same paths Makefile/compose use.
_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "packages", _REPO / "apps/api", _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.eval import (  # noqa: E402 - must follow sys.path insert above
    EvaluationDataset,
    GoldenExample,
    MetricRow,
    RetrievalResult,
    build_comparison_report,
    evaluate_dataset,
)

GOLDEN = Path(__file__).with_name("golden_smoke.jsonl")
RUN_ID = "smoke-fake"

# Fake ground truth mirror (doc ids the fake retriever will return). The
# multi-doc question deliberately cites one fabricated doc to exercise
# citation_accuracy. Context is only set for the answered EN row so
# faithfulness is non-trivial.
_GT = {
    "What is the termination notice period?": ["doc-en"],
    "Care este perioada de preaviz?": ["doc-ro"],
    "Qual e o periodo de aviso previo?": ["doc-pt"],
    "What is the Q3 net revenue?": ["doc-table"],
    "Which two firms merged in 2019?": ["doc-a", "doc-b"],
}
_CITED = {"Which two firms merged in 2019?": ["doc-a", "doc-fabricated"]}
_ANSWER_CTX = {
    "What is the termination notice period?": "The termination notice period is 30 days."
}


def load_dataset() -> EvaluationDataset:
    examples = [
        GoldenExample(**json.loads(line))
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return EvaluationDataset(id="ds-smoke", name="golden_smoke", examples=examples)


def retrieve(question: str) -> RetrievalResult:
    docs = _GT[question]
    return RetrievalResult(
        doc_ids=docs,
        context=_ANSWER_CTX.get(question, ""),
        cited_doc_ids=_CITED.get(question, docs),
        answer=_ANSWER_CTX.get(question, ""),
    )


async def main() -> None:
    dataset = load_dataset()
    run = await evaluate_dataset(dataset, retrieve, run_id=RUN_ID)
    print(f"dataset={dataset.name} n_questions={run.config_snapshot['n_questions']}")
    for metric in (
        "recall",
        "precision",
        "mrr",
        "ndcg",
        "citation_accuracy",
        "faithfulness",
    ):
        print(f"  {metric:18s} {run.results[metric]:.4f}")
    rows = [
        MetricRow(
            question=ex.question,
            run_id=RUN_ID,
            **{m: run.results[m] for m in run.results},
        )
        for ex in dataset.examples
    ]
    report = build_comparison_report(rows)
    summary = report.runs[0]
    print(f"comparison: run={summary.run_id} n={summary.n_questions} means={summary.means}")


if __name__ == "__main__":
    asyncio.run(main())
