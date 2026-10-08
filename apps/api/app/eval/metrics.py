"""Evaluation metrics — retrieval ranking + groundedness (TASK-M8-001).

Deterministic, unit-testable metric math. No LLM is required for the ranking
and citation metrics (recall/precision/MRR/NDCG/citation-accuracy are pure set
math over document ids). Faithfulness is groundedness between an answer and its
supporting context; the default scorer is a deterministic lexical-overlap proxy
so the harness runs offline, and a real RAGAS/LLM scorer can be injected
without touching callers (see :func:`score_faithfulness`).

Reuse note: RAGAS (Apache-2.0) is the intended production scorer for
faithfulness; we keep our own deterministic implementation behind an injectable
seam so the harness is hermetic in CI and swappable to RAGAS at runtime.
"""

from __future__ import annotations

from collections.abc import Callable

# A groundedness scorer maps (answer, context) -> [0, 1]. RAGAS or any LLM
# judge can be dropped in here; the default is the lexical proxy below.
FaithfulnessScorer = Callable[[str, str], float]


def compute_recall(predicted_docs: list[str], ground_truth_docs: list[str]) -> float:
    """Fraction of ground-truth docs that appear in the retrieved set."""
    pred_set = set(predicted_docs)
    gt_set = set(ground_truth_docs)
    if not gt_set:
        return 0.0
    return len(pred_set & gt_set) / len(gt_set)


def compute_precision(predicted_docs: list[str], ground_truth_docs: list[str]) -> float:
    """Fraction of retrieved docs that are relevant (in ground truth)."""
    pred_set = set(predicted_docs)
    gt_set = set(ground_truth_docs)
    if not pred_set:
        return 0.0
    return len(pred_set & gt_set) / len(pred_set)


def compute_mrr(predicted_docs: list[str], ground_truth_docs: list[str]) -> float:
    """Mean reciprocal rank of the first relevant doc (single-query form).

    Uses rank order (duplicates collapsed, first occurrence wins); returns
    ``1/rank`` of the earliest ground-truth hit, else 0.0.
    """
    gt_set = set(ground_truth_docs)
    if not gt_set:
        return 0.0
    seen: set[str] = set()
    for rank, doc in enumerate(predicted_docs, start=1):
        if doc in seen:
            continue
        seen.add(doc)
        if doc in gt_set:
            return 1.0 / rank
    return 0.0


def compute_ndcg(
    predicted_docs: list[str],
    ground_truth_docs: list[str],
) -> float:
    """Binary-relevance NDCG over document ids.

    Gain per position is 1 for a relevant, not-yet-counted doc; DCG is
    log2-discounted. IDCG is the ideal ordering (all relevant first). Returns
    1.0 when everything relevant is ranked first, 0.0 with no relevance.
    """
    import math

    gt_set = set(ground_truth_docs)
    if not gt_set:
        return 0.0

    dcg = 0.0
    seen: set[str] = set()
    for rank, doc in enumerate(predicted_docs, start=1):
        if doc in seen:
            continue
        seen.add(doc)
        if doc in gt_set:
            dcg += 1.0 / math.log2(rank + 1)

    ideal_hits = min(len(gt_set), len(set(predicted_docs)))
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def compute_citation_accuracy(
    cited_docs: list[str],
    ground_truth_docs: list[str],
) -> float:
    """Fraction of cited docs that are actually relevant (no fabrication).

    Penalizes fabricated citations: a cited doc outside ground truth lowers the
    score. Returns 0.0 when nothing was cited (no verifiable citations).
    """
    cited_set = set(cited_docs)
    if not cited_set:
        return 0.0
    gt_set = set(ground_truth_docs)
    return len(cited_set & gt_set) / len(cited_set)


def _lexical_faithfulness(answer: str, context: str) -> float:
    """Deterministic groundedness proxy: token overlap of answer vs context.

    Normalizes case + strips punctuation, then returns
    ``|answer ∩ context| / |answer|`` (content tokens). Offline + hermetic;
    RAGAS/LLM scorers replace this via :func:`score_faithfulness`.
    """
    import re

    def tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    answer_tokens = tokens(answer)
    if not answer_tokens:
        return 0.0
    context_tokens = tokens(context)
    return len(answer_tokens & context_tokens) / len(answer_tokens)


def score_faithfulness(
    answer: str,
    context: str,
    scorer: FaithfulnessScorer | None = None,
) -> float:
    """Groundedness of ``answer`` against ``context`` in [0, 1].

    Uses the injected ``scorer`` (e.g. RAGAS) when provided, else the
    deterministic lexical proxy. Clamped to [0, 1] so a custom scorer can never
    emit an out-of-range value into the report.
    """
    raw = scorer(answer, context) if scorer is not None else _lexical_faithfulness(
        answer, context
    )
    if raw != raw:  # NaN guard
        return 0.0
    return max(0.0, min(1.0, raw))
