"""T-EVAL-007 — leaderboard-comparable harness: covering tests.

Metric sanity (known-answer 1.0 / known-wrong 0.0), structural column
separation, vendored-code provenance, and placeholder clarity.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from evals import leaderboard as lb
from evals.vendor import (
    OFFICIAL_METRIC_SOURCES,
    VENDOR_PROVENANCE,
    OfficialMetricNotVendored,
    placeholder_file,
    placeholders,
    vendored_source_for,
)
from evals.vendor import hotpot_evaluate_v1 as hotpot
from evals.vendor import squad_evaluate_v2 as squad

# --------------------------------------------------------------------------- #
# Metric sanity: known-answer fixtures score 1.0, known-wrong score 0.0
# --------------------------------------------------------------------------- #

GOLD_ANSWER = "The budget is 900 dollars."
GOLD_FACTS = [["Budget Act", 0], ["Finance Report", 3]]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fn", "args"),
    [
        (hotpot.answer_em, (GOLD_ANSWER, GOLD_ANSWER)),
        (hotpot.answer_f1, (GOLD_ANSWER, GOLD_ANSWER)),
        (hotpot.supporting_fact_em, (GOLD_FACTS, GOLD_FACTS)),
        (hotpot.supporting_fact_precision, (GOLD_FACTS, GOLD_FACTS)),
        (hotpot.supporting_fact_recall, (GOLD_FACTS, GOLD_FACTS)),
        (hotpot.supporting_fact_f1, (GOLD_FACTS, GOLD_FACTS)),
        (hotpot.joint_em, (GOLD_ANSWER, GOLD_ANSWER, GOLD_FACTS, GOLD_FACTS)),
        (squad.squad_exact_match, (GOLD_ANSWER, [GOLD_ANSWER])),
        (squad.squad_f1, (GOLD_ANSWER, [GOLD_ANSWER])),
    ],
)
def test_known_answer_scores_one(fn, args) -> None:
    assert fn(*args) == 1.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fn", "args"),
    [
        (hotpot.answer_em, (GOLD_ANSWER, "The budget is 1900 dollars.")),
        (hotpot.answer_f1, (GOLD_ANSWER, "Cats are unrelated entirely.")),
        (hotpot.supporting_fact_em, (GOLD_FACTS, [["Wrong Title", 9]])),
        (hotpot.supporting_fact_precision, (GOLD_FACTS, [["Wrong Title", 9]])),
        (hotpot.supporting_fact_recall, (GOLD_FACTS, [["Wrong Title", 9]])),
        (hotpot.supporting_fact_f1, (GOLD_FACTS, [["Wrong Title", 9]])),
        (hotpot.joint_em, (GOLD_ANSWER, "Cats are unrelated entirely.", GOLD_FACTS, GOLD_FACTS)),
        (squad.squad_exact_match, (GOLD_ANSWER, ["The budget is 1900 dollars."])),
        (squad.squad_f1, (GOLD_ANSWER, ["Cats are unrelated entirely."])),
    ],
)
def test_known_wrong_scores_zero(fn, args) -> None:
    assert fn(*args) == 0.0


@pytest.mark.unit
def test_partial_answers_score_between() -> None:
    """A partial answer is neither 1.0 nor 0.0 (the F1 curve is real)."""
    score = hotpot.answer_f1("The budget is 900", GOLD_ANSWER)
    assert 0.0 < score < 1.0


@pytest.mark.unit
def test_empty_answer_f1_convention_matches_reference() -> None:
    """HotpotQA convention: two no-answers agree -> F1 1; one empty -> 0."""
    assert hotpot.answer_f1("", "") == 1.0
    assert hotpot.answer_f1("", GOLD_ANSWER) == 0.0
    assert hotpot.answer_f1(GOLD_ANSWER, "") == 0.0


@pytest.mark.unit
def test_squad_max_over_ground_truths() -> None:
    """SQuAD aggregates over the gold answer set (max wins)."""
    assert squad.squad_f1(GOLD_ANSWER, ["nothing", GOLD_ANSWER]) == 1.0
    assert squad.squad_exact_match(GOLD_ANSWER, ["nothing", GOLD_ANSWER]) == 1.0


# --------------------------------------------------------------------------- #
# Official/ours separation — the blend metric
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_columns_structurally_separated() -> None:
    row = lb.LeaderboardRow(dataset="hotpotqa", model="ours")
    row.official["answer_em"] = lb.run_official("answer_em", GOLD_ANSWER, GOLD_ANSWER)
    row.ours["retrieval_recall"] = lb.run_ours("retrieval_recall", ["a"], ["a"])
    lb.assert_columns_separate(row)  # no blend
    with pytest.raises(AssertionError):
        row.ours["answer_em"] = row.official["answer_em"]
        lb.assert_columns_separate(row)


@pytest.mark.unit
def test_ours_only_columns_never_leak_into_official() -> None:
    """Every ours metric is mapped OURS_ONLY; every official metric OFFICIAL."""
    for name in lb.OURS_METRIC_FNS:
        assert lb.MAPPING[name] == lb.OURS_ONLY
        assert lb.metric_spec(name).official_fn is None
    for name in OFFICIAL_METRIC_SOURCES:
        assert lb.MAPPING[name] == lb.OFFICIAL


@pytest.mark.unit
def test_official_metrics_resolve_to_vendored_code_only() -> None:
    """reimplemented-and-called-official metrics: 0 — the official callables
    live in the vendored vendor modules, nowhere else."""
    for name in (
        "answer_em", "answer_f1", "supporting_fact_f1", "joint_em",
        "squad_f1", "squad_exact_match",
    ):
        fn = lb.metric_spec(name).official_fn
        assert fn is not None
        assert fn.__module__ in {
            "evals.vendor.hotpot_evaluate_v1",
            "evals.vendor.squad_evaluate_v2",
        }, f"{name} official value would not come from vendored code"
    # Our own recall/precision primitives are a different surface entirely.
    assert lb.OURS_METRIC_FNS["retrieval_recall"].__module__ == "app.eval.metrics"


@pytest.mark.unit
def test_table_shape_matches_leaderboard_precision_and_pending() -> None:
    row = lb.LeaderboardRow(dataset="hotpotqa", model="ours")
    row.official["answer_em"] = lb.run_official("answer_em", GOLD_ANSWER, GOLD_ANSWER)
    row.official["twowiki_answer_em"] = lb.run_official("twowiki_answer_em", GOLD_ANSWER, GOLD_ANSWER)  # noqa: E501
    table = lb.shape_table([row], ["answer_em", "twowiki_answer_em"], ["retrieval_recall"])
    assert table[0] == ["dataset", "model", "answer_em", "twowiki_answer_em", "retrieval_recall"]
    assert table[1][2] == "1.0"  # official, one decimal
    assert table[1][3] == "pending"  # not-yet-landed official, never 0.0
    # ours column absent from the run renders pending too (not fabricated)
    assert table[1][4] == "pending"


# --------------------------------------------------------------------------- #
# Placeholders — clearly marked, never a number
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    "fn",
    [
        placeholders.twowiki_answer_em,
        placeholders.twowiki_answer_f1,
        placeholders.musique_answer_em,
        placeholders.musique_answer_f1,
        placeholders.bfcl_ast_accuracy,
    ],
)
def test_placeholders_refuse_with_ticket_named(fn) -> None:
    with pytest.raises(OfficialMetricNotVendored) as exc:
        fn(GOLD_ANSWER, GOLD_ANSWER)
    message = str(exc.value)
    assert "T-EVAL-" in message  # the landing ticket is named
    assert "not vendored" in message


@pytest.mark.unit
def test_placeholder_registry_entries_are_pending() -> None:
    for source in VENDOR_PROVENANCE:
        if source.status == "placeholder":
            assert source.pending_ticket is not None
            assert source.fetch
            for metric in source.metrics:
                assert lb.official_pending(metric) is True
                assert placeholder_file(metric) == f"evals/vendor/placeholders.py::{source.name}"


@pytest.mark.unit
def test_vendored_entries_carry_provenance() -> None:
    for source in VENDOR_PROVENANCE:
        assert source.upstream and source.license
        if source.status == "vendored":
            for metric in source.metrics:
                assert lb.official_pending(metric) is False
                assert metric in OFFICIAL_METRIC_SOURCES
    assert vendored_source_for("answer_em").name == "hotpot_evaluate_v1"
    assert vendored_source_for("squad_f1").name == "squad_evaluate_v2"


@pytest.mark.unit
def test_mapping_doc_agrees_with_the_registry() -> None:
    """The mapping doc classifies every registered metric exactly once."""
    doc = (Path(__file__).resolve().parents[2] / "evals" / "MAPPING.md").read_text()
    # Grouped forms in the doc (e.g. `supporting_fact_em/precision/recall/f1`)
    # cover their members.
    documented = set()
    for match in re.finditer(r"`([a-z0-9_]+(?:/[a-z0-9_]+)*)`", doc):
        token = match.group(1)
        documented.add(token)
        for name in token.split("/"):
            documented.add(name)
    for metric, kind in lb.MAPPING.items():
        assert metric in documented, f"{metric} ({kind}) is not in MAPPING.md"
        if kind == lb.OFFICIAL:
            assert metric in OFFICIAL_METRIC_SOURCES
        else:
            assert metric in lb.OURS_METRIC_FNS
    assert "never blended into an official" in doc
