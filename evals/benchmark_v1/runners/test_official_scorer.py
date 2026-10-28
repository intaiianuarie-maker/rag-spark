"""Fixture test for the vendored OFFICIAL HotpotQA scorer.

Required before any scoring: prove the vendored `hotpot_evaluate_v1` behaves
correctly on hand-checked cases, so a scorecard number can be trusted to mean what
it claims. Nothing here is model output; these are literals with known answers.

Cases cover: normalisation (articles/punctuation/case/whitespace), token-F1
partial credit, exact match, and the supporting-fact scorer on (title, sentence_id)
pairs.
"""

from __future__ import annotations

import pytest
from hotpot_evaluate_v1 import (
    answer_em,
    answer_f1,
    joint_f1,
    normalize_answer,
    supporting_fact_f1,
)

pytestmark = pytest.mark.unit


def test_normalize_strips_articles_punct_case_and_whitespace() -> None:
    assert normalize_answer("The  Beatles!") == "beatles"
    assert normalize_answer("A B  C.") == "b c"
    assert normalize_answer("  Paris  ") == "paris"
    # Numbers survive normalisation; this is what the 900/1900 guard relies on.
    assert normalize_answer("$1,900.") == "1900"


def test_answer_exact_match() -> None:
    assert answer_em("Paris", "paris") == 1.0
    assert answer_em("Paris", "The answer is Paris") == 0.0
    assert answer_em("yes", "Yes") == 1.0


def test_answer_f1_gives_partial_credit() -> None:
    # gold {paris, is, capital}; pred shares 2 of 3 gold tokens, pred has 3.
    f1 = answer_f1("Paris is the capital", "Paris is a city")
    assert 0.0 < f1 < 1.0
    assert answer_f1("Paris", "paris") == 1.0
    assert answer_f1("Paris", "London") == 0.0


def test_answer_f1_is_symmetric_for_same_token_multiset() -> None:
    """The official F1 is symmetric; the harness must not score order-sensitively."""
    a, b = "Acme acquired Globex", "Globex acquired Acme"
    assert answer_f1(a, b) == pytest.approx(answer_f1(b, a))


def test_supporting_fact_f1_on_title_sentence_pairs() -> None:
    gold = [["Acme Corp", "0"], ["Globex", "2"]]
    exact = supporting_fact_f1(gold, [["Acme Corp", "0"], ["Globex", "2"]])
    partial = supporting_fact_f1(gold, [["Acme Corp", "0"]])
    wrong_title = supporting_fact_f1(gold, [["Nope Inc", "0"], ["Globex", "2"]])
    assert exact == 1.0
    assert 0.0 < partial < 1.0
    # The official scorer compares the WHOLE (title, sent_id) pair: predicting the
    # wrong title for one fact loses that fact, while the exact match keeps its
    # half of the credit -> 0.5, not 0.0. Pinned so a regression in pair-matching
    # (e.g. scoring title only) is caught.
    assert wrong_title == 0.5


def test_joint_f1_uses_both_answer_and_facts() -> None:
    gold_facts = [["Acme Corp", "0"]]
    pred_facts = [["Acme Corp", "0"]]
    assert joint_f1("yes", "yes", gold_facts, pred_facts) == 1.0
    # Perfect answer, no facts -> joint is not perfect.
    assert joint_f1("yes", "yes", gold_facts, []) < 1.0


def test_empty_prediction_scores_zero_not_crash() -> None:
    assert answer_em("Paris", "") == 0.0
    assert answer_f1("Paris", "") == 0.0
    assert supporting_fact_f1([["A", "0"]], []) == 0.0