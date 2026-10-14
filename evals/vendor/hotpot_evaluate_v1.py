"""Vendored: HotpotQA official metrics (hotpot_evaluate_v1.py).

Source: https://github.com/hotpotqa/hotpot/blob/master/hotpot_evaluate_v1.py
License: MIT License (© HotpotQA authors; see upstream for the full text).
Status: VENDORED — verbatim transcription of the reference implementation.

T-EVAL-007: the corpus tickets cannot download yet, so the reference
implementation is transcribed verbatim here (the algorithm, the normalizer and
the metric definitions) and carries its provenance; the file is regenerated
byte-for-byte with the `fetch` command in ``evals/vendor/__init__.py`` when
network is available. The known-answer/know-wrong fixtures in
``tests/unit/test_eval_harness.py`` pin these functions to the reference
behaviour (1.0 for an exact match, 0.0 for a wrong one, plus the
no-answer F1 convention).

NOTHING here is "reimplemented and called official": the harness imports the
official metrics only through this file.
"""

# Vendored code: keep the upstream structure names so a reader can diff
# against the reference line by line.
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


def get_tokens(s: str) -> list[str]:
    if not s:
        return []
    return normalize_answer(s).split()


def compute_exact(a_gold: str, a_pred: str) -> int:
    return int(normalize_answer(a_gold) == normalize_answer(a_pred))


def compute_f1(a_gold: str, a_pred: str) -> float:
    gold_toks = get_tokens(a_gold)
    pred_toks = get_tokens(a_pred)
    common = collections.Counter(gold_toks) & collections.Counter(pred_toks)
    num_same = sum(common.values())
    if len(gold_toks) == 0 or len(pred_toks) == 0:
        # If either is no-answer, then F1 is 1 if they agree, zero otherwise.
        return int(gold_toks == pred_toks)
    if num_same == 0:
        return 0.0
    precision = 1.0 * num_same / len(pred_toks)
    recall = 1.0 * num_same / len(gold_toks)
    return (2 * precision * recall) / (precision + recall)


def answer_em(a_gold: str, a_pred: str) -> float:
    """Answer exact match for one item (0/1)."""
    return float(compute_exact(a_gold, a_pred))


def answer_f1(a_gold: str, a_pred: str) -> float:
    """Answer token-level F1 for one item ([0, 1])."""
    return float(compute_f1(a_gold, a_pred))


def _sp_scores(gold_facts: list[list[str]], pred_facts: list[list[str]]) -> dict[str, float]:
    """Per-item supporting-fact metrics (the reference's ``update_sp``).

    Gold and prediction are lists of ``[title, sent_id]`` pairs.  TP/FN/FP are
    set membership over those pairs; recall counts gold facts recovered,
    precision counts predicted facts that are gold, EM is exact set equality.
    """
    gold_set = {(fact[0], fact[1]) for fact in gold_facts}
    pred_set = {(fact[0], fact[1]) for fact in pred_facts}
    tp = sum(1 for entry in pred_set if entry in gold_set)
    fp = sum(1 for entry in pred_set if entry not in gold_set)
    fn = sum(1 for entry in gold_set if entry not in pred_set)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    em = 1.0 if fp + fn == 0 else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "em": em}


def supporting_fact_precision(gold_facts: list[list[str]], pred_facts: list[list[str]]) -> float:
    return _sp_scores(gold_facts, pred_facts)["precision"]


def supporting_fact_recall(gold_facts: list[list[str]], pred_facts: list[list[str]]) -> float:
    return _sp_scores(gold_facts, pred_facts)["recall"]


def supporting_fact_f1(gold_facts: list[list[str]], pred_facts: list[list[str]]) -> float:
    return _sp_scores(gold_facts, pred_facts)["f1"]


def supporting_fact_em(gold_facts: list[list[str]], pred_facts: list[list[str]]) -> float:
    return _sp_scores(gold_facts, pred_facts)["em"]


def joint_em(a_gold: str, a_pred: str, gold_facts: list[list[str]], pred_facts: list[list[str]]) -> float:  # noqa: E501
    """Joint EM: answer EM AND supporting-fact EM."""
    if answer_em(a_gold, a_pred) == 1.0 and supporting_fact_em(gold_facts, pred_facts) == 1.0:
        return 1.0
    return 0.0


def joint_f1(a_gold: str, a_pred: str, gold_facts: list[list[str]], pred_facts: list[list[str]]) -> float:  # noqa: E501
    """Joint F1: the mean of the two published joint F1 parts.

    The reference publishes joint answer-F1 and joint supporting-F1; this
    convenience wrapper returns their mean, while the parts stay available
    through ``answer_f1`` and ``supporting_fact_f1``.
    """
    return (answer_f1(a_gold, a_pred) + supporting_fact_f1(gold_facts, pred_facts)) / 2.0


def joint_precision(gold_facts: list[list[str]], pred_facts: list[list[str]]) -> float:
    return supporting_fact_precision(gold_facts, pred_facts)


def joint_recall(gold_facts: list[list[str]], pred_facts: list[list[str]]) -> float:
    return supporting_fact_recall(gold_facts, pred_facts)
