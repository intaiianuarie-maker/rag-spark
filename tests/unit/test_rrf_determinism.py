"""RRF determinism on canned scores (TASK-M0-004 requirement 2).

Runs the reusable checker `retrieval.contract_tests.check_rrf_determinism`
against the harness reference fusion and then proves the checker has teeth by
feeding it a deliberately unstable fusion. M3-001b's real fusion must pass the
same checker unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import pytest
from harness_fakes import RRF_K, reference_rrf, sort_fused
from retrieval.contract_tests import check_rrf_determinism

pytestmark = pytest.mark.unit

# Canned per-retriever scores: d2/d3 tie, b-1 wins the dense list only.
CANNED_DENSE = {"b-1": 0.99, "d1": 0.91, "d2": 0.75, "d3": 0.75}
CANNED_LEXICAL = {"d1": 4.0, "d3": 3.0, "d2": 1.0, "b-1": 1.0}


def ranks_from_scores(canned: Mapping[str, float]) -> list[str]:
    """Score desc, id asc — the only ranking rule the debugger may rely on."""
    return sorted(canned, key=lambda doc_id: (-canned[doc_id], doc_id))


def test_fusion_scores_match_the_rrf_definition() -> None:
    fused = reference_rrf([["d1", "d2"], ["d2", "d1"]])
    assert math.isclose(fused["d1"], 1 / (RRF_K + 1) + 1 / (RRF_K + 2), rel_tol=1e-15)
    assert fused["d1"] == fused["d2"], "symmetric rankings must fuse to equal scores"


def test_fusion_is_stable_across_repeated_calls_on_canned_scores() -> None:
    rankings = [ranks_from_scores(CANNED_DENSE), ranks_from_scores(CANNED_LEXICAL)]
    baseline = reference_rrf(rankings)
    for _ in range(25):
        assert reference_rrf(rankings) == baseline
    assert sort_fused(baseline) == sort_fused(baseline)


def test_fusion_ignores_the_order_the_retrievers_return_in() -> None:
    rankings = [ranks_from_scores(CANNED_DENSE), ranks_from_scores(CANNED_LEXICAL)]
    forward = reference_rrf(rankings)
    backward = reference_rrf(list(reversed(rankings)))
    assert set(forward) == set(backward)
    for doc_id, value in forward.items():
        assert backward[doc_id] == value, f"{doc_id} fused score depends on retriever order"


def test_ties_break_deterministically_on_ids_not_dict_order() -> None:
    fused = reference_rrf([ranks_from_scores(CANNED_DENSE)])
    ranked = sort_fused(fused)
    assert ranked == sorted(ranked, key=lambda doc_id: (-fused[doc_id], doc_id))
    assert ranked.index("d2") < ranked.index("d3"), "score ties must break by id"


def test_fusion_of_no_rankings_is_empty_and_invalid_k_is_rejected() -> None:
    assert reference_rrf([]) == {}
    with pytest.raises(ValueError):
        reference_rrf([["d1"]], k=0)


def test_harness_reference_passes_the_reusable_determinism_checker() -> None:
    check_rrf_determinism(reference_rrf)


def test_determinism_checker_rejects_an_unstable_fusion() -> None:
    """Negative control: the checker must fail a non-deterministic implementation."""
    calls = {"n": 0}

    def unstable(rankings: Sequence[Sequence[str]]) -> dict[str, float]:
        calls["n"] += 1
        offset = 1.0 if calls["n"] % 2 else 0.0
        return {doc_id: offset for ranking in rankings for doc_id in ranking}

    with pytest.raises(AssertionError):
        check_rrf_determinism(unstable)
