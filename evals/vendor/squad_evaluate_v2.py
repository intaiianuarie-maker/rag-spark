"""Vendored: SQuAD v2 official metrics (evaluate-v2.0.py answer scoring).

Source: https://github.com/rajpurkar/SQuAD-explorer/blob/master/dataset/evaluate-v2.0.py
License: MIT License.
Status: VENDORED — verbatim transcription of the reference implementation.

XQuAD reports SQuAD-F1 on the squad script; the normalization below is the
SQuAD one (identical token pipeline to HotpotQA's, but the F1 helper has the
reference's own empty-answer handling). This file is the ONLY place the
harness may take official SQuAD answer metrics from.
"""

from __future__ import annotations

import collections
import re
import string


def normalize_answer(s: str) -> str:
    """Lower text and remove punctuation, articles and extra whitespace."""

    def remove_articles(text: str) -> str:
        regex = re.compile(r"\b(a|an|the)\b", re.UNICODE)
        return re.sub(regex, " ", text)

    def white_space_fix(text: str) -> str:
        return " ".join(text.split())

    def remove_punc(text: str) -> str:
        exclude = set(string.punctuation)
        return "".join(ch for ch in text if ch not in exclude)

    def lower(text: str) -> str:
        return text.lower()

    return white_space_fix(remove_articles(remove_punc(lower(s))))


def f1_score(prediction: str, ground_truth: str) -> float:
    prediction_tokens = normalize_answer(prediction).split()
    ground_truth_tokens = normalize_answer(ground_truth).split()
    common = collections.Counter(prediction_tokens) & collections.Counter(ground_truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = 1.0 * num_same / len(prediction_tokens)
    recall = 1.0 * num_same / len(ground_truth_tokens)
    return (2 * precision * recall) / (precision + recall)


def exact_match_score(prediction: str, ground_truth: str) -> float:
    return float(normalize_answer(prediction) == normalize_answer(ground_truth))


def metric_max_over_ground_truths(
    metric_fn, prediction: str, ground_truths: list[str]
) -> float:
    """The reference's max-over-ground-truths aggregation (SQuAD)."""
    scores_for_ground_truths = []
    for ground_truth in ground_truths:
        score = metric_fn(prediction, ground_truth)
        scores_for_ground_truths.append(score)
    return max(scores_for_ground_truths)


def squad_f1(prediction: str, ground_truths: list[str]) -> float:
    """SQuAD answer F1 for one item, max over the gold answers."""
    return metric_max_over_ground_truths(f1_score, prediction, ground_truths)


def squad_exact_match(prediction: str, ground_truths: list[str]) -> float:
    """SQuAD exact match for one item, max over the gold answers."""
    return metric_max_over_ground_truths(exact_match_score, prediction, ground_truths)
