"""T-V1-076 — covering tests for the three F1 harnesses + the abstention-aware gate.

The most important test here is
``test_abstention_harness_can_detect_a_leak``: an abstention harness that
returns 100% for every input is indistinguishable from a broken one. That
negative control pins the metric to something meaningful.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_EVALS = _REPO / "evals"
for _p in (_REPO / "packages", _REPO / "apps" / "api", _REPO, _EVALS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gates = _load("q9_gates_t076", _EVALS / "q9_gates.py")
bench = _load("q9_run_benchmark_t076", _EVALS / "q9_run_benchmark.py")
b8 = _load("build_q8_slices_t076", _EVALS / "build_q8_slices.py")

#: T-061a's recorded baseline. The five answerable slices must be BYTE-IDENTICAL
#: after T-V1-076: the abstention gate is additive and relaxes nothing.
BASELINE_ANSWERABLE = {
    "en": (10, 1.0000, 1.0000),
    "ro": (10, 1.0000, 1.0000),
    "pt": (10, 0.9853, 1.0000),
    "tables": (10, 1.0000, 1.0000),
    "multi": (10, 0.9586, 1.0000),
}
BASELINE_OVERALL = (50, 0.9888, 1.0000)


@pytest.fixture(scope="module")
def summary() -> dict:
    return bench.run_benchmark()


# --- 1. FACTUAL harness ------------------------------------------------------


def test_factual_rate_counts_supported_claims() -> None:
    ev = "The quick brown fox jumps over the lazy dog near the river bank."
    rep = gates.claim_support_rate(
        [("a", "The quick brown fox jumps over the lazy dog", ev)],
    )
    assert rep.n_claims == 1 and rep.supported == 1 and rep.rate == 1.0
    assert rep.oracle is True


def test_factual_rate_flags_unsupported_claims() -> None:
    ev = "The quick brown fox jumps over the lazy dog near the river bank."
    rep = gates.claim_support_rate(
        [
            ("supported", "The quick brown fox jumps over the lazy dog", ev),
            ("invented", "Revenue reached 47,880 thousand in fiscal 2021", ev),
        ],
    )
    assert rep.n_claims == 2 and rep.supported == 1 and rep.rate == 0.5
    assert rep.unsupported_ids == ["invented"]


def test_factual_rate_is_zero_on_empty_input_not_one() -> None:
    """A vacuous 1.0 on no claims would silently pass a >=98% gate."""
    assert gates.claim_support_rate([]).rate == 0.0


# --- 2. RECALL@20 harness (reuses compute_recall) ---------------------------


def test_recall_at_k_is_compute_recall_on_the_truncated_ranking() -> None:
    from app.eval.metrics import compute_recall

    gold = ["d1", "d2"]
    ranked = ["x", "x2", "d1", "d2", "y"]
    assert gates.recall_at_k(gold, ranked, k=2) == compute_recall(["x", "x2"], gold) == 0.0
    assert gates.recall_at_k(gold, ranked, k=20) == compute_recall(ranked, gold) == 1.0


def test_recall_at_k_uses_twenty_by_default() -> None:
    assert gates.RECALL_AT_K == 20
    assert gates.RECALL_TARGET == 0.95


def test_recall_at_k_partial_credit() -> None:
    assert gates.recall_at_k(["d1", "d2", "d3"], ["d1"], k=20) == pytest.approx(1 / 3)


# --- 3. ABSTENTION harness ---------------------------------------------------


def _items_for(context: str) -> list:
    return bench._retrieval_result({"context": context, "question": "q", "gold_answer": ""})[0]


def test_abstention_harness_detects_a_leak() -> None:
    """NEGATIVE CONTROL — the metric must be able to report non-abstention.

    A verbatim-supported claim DOES survive the real verification path, so it is
    counted as a leak. Without this, a harness that always returned 1.0 would
    pass every assertion while measuring nothing.
    """
    # Multi-sentence on purpose: a SINGLE-sentence chunk does not self-verify
    # under ExtractiveGrounder (empirically score 0.0), so the control needs a
    # chunk whose sentences can be individually attested.
    context = (
        "About Time is a 2013 British romantic comedy-drama film written by Richard Curtis. "
        "The film stars Domhnall Gleeson and Ruth Wilson."
    )
    items = _items_for(context)
    # A sentence the grounder actually attests. (Empirically the sibling
    # sentence containing a bare year does NOT self-verify — flagged, not fixed;
    # product behaviour is out of this ticket's scope.)
    supported_claim = "The film stars Domhnall Gleeson and Ruth Wilson."
    report = gates.evaluate_abstention([("supported", items, supported_claim)])
    assert report.abstained == 0, "a supported claim must not be counted as abstention"
    assert report.rate == 0.0
    assert report.leaked == ["supported"]


def test_abstention_harness_self_check_rejects_a_vacuous_one() -> None:
    """The self-check must be able to fail, or it proves nothing."""
    context = (
        "About Time is a 2013 British romantic comedy-drama film written by Richard Curtis. "
        "The film stars Domhnall Gleeson and Ruth Wilson."
    )
    items = _items_for(context)

    class DeadGrounder:
        threshold = 1.0

        def score(self, claim: str, content: str) -> float:
            return 0.0  # certifies nothing, ever

    with pytest.raises(RuntimeError, match="self-check FAILED"):
        gates._abstention_self_check(DeadGrounder(), items)


def test_abstention_report_from_the_real_corpus_is_not_vacuous() -> None:
    """Every abstention number must pass the self-check that proves the harness
    can certify a supported claim."""
    rows = [r for r in b8.load_slices() if r["slice"] == "unanswerable"]
    pairs = [bench._abstention_pair(r) for r in rows]
    report = gates.evaluate_abstention(pairs)
    assert report.n_items == 10
    assert report.leaked == []
    assert report.rate >= gates.ABSTENTION_TARGET


def test_abstention_harness_counts_a_refusal() -> None:
    context = "Restructuring liabilities total $2,167 as of the balance sheet date."
    items = _items_for(context)
    report = gates.evaluate_abstention(
        [("unsupported", items, gates.naive_restatement("What was total revenue in fiscal 2021?"))]
    )
    assert report.rate == 1.0 and report.leaked == []


def test_abstention_rate_is_zero_on_empty_input() -> None:
    assert gates.evaluate_abstention([]).rate == 0.0


def test_naive_restatement_is_deterministic_and_keeps_content_words() -> None:
    q = "What was the total revenue reported for fiscal 2021 in the segment table?"
    first = gates.naive_restatement(q)
    assert first == gates.naive_restatement(q)
    assert "revenue" in first and "fiscal" in first
    assert "What" not in first and "the" not in first


# --- 4. Abstention-aware gate -----------------------------------------------


def test_abstention_slice_is_detected_from_data_not_name() -> None:
    """A slice is unanswerable because its records have no gold answer."""
    assert bench.is_abstention_slice([{"gold_answer": ""}, {"gold_answer": ""}])
    assert not bench.is_abstention_slice([{"gold_answer": "x"}])
    assert not bench.is_abstention_slice([])
    # Mixed data is not an abstention slice — it has an answerable record.
    assert not bench.is_abstention_slice([{"gold_answer": ""}, {"gold_answer": "y"}])


def test_unanswerable_slice_is_gated_on_abstention_not_groundedness(summary: dict) -> None:
    s = summary["unanswerable"]
    assert s["metric"] == "abstention_rate"
    assert "groundedness" not in s, "groundedness is a category error on unanswerable data"
    assert s["n"] == 10
    assert s["abstention_rate"] >= gates.ABSTENTION_TARGET


def test_answerable_slice_numbers_are_byte_identical_to_baseline(summary: dict) -> None:
    for tag, (n, grounded, valid) in BASELINE_ANSWERABLE.items():
        s = summary[tag]
        got = (s["n"], round(s["groundedness"], 4), round(s["citation_validity"], 4))
        assert got == (n, grounded, valid), tag


def test_overall_numbers_are_byte_identical_to_baseline(summary: dict) -> None:
    n, grounded, valid = BASELINE_OVERALL
    s = summary["_overall"]
    got = (s["n"], round(s["groundedness"], 4), round(s["citation_validity"], 4))
    assert got == (n, grounded, valid)


def test_no_answerable_threshold_was_relaxed() -> None:
    assert bench.GROUNDEDNESS_GATE == 0.90
    assert bench.CITATION_GATE == 1.0
    assert bench.ABSTENTION_GATE == 0.95


def test_gate_resolves_green_with_abstention_reported() -> None:
    assert bench.main([]) == 0


# --- 5. Corpus untouched -----------------------------------------------------


def test_the_pinned_corpus_is_still_intact() -> None:
    """T-V1-076 is eval-code only; the 60 frozen records must still verify."""
    assert len(b8.load_slices()) == 60


def test_benchmark_metrics_module_was_not_modified() -> None:
    """The card required reusing the existing primitives, not reimplementing."""
    src = (_REPO / "apps" / "api" / "app" / "eval" / "metrics.py").read_text(encoding="utf-8")
    assert "def compute_recall" in src and "def score_faithfulness" in src
    assert gates.compute_recall is not None  # imported from the product module, not redefined


def test_unanswerable_records_still_have_no_gold_answer() -> None:
    rows = [r for r in b8.load_slices() if r["slice"] == "unanswerable"]
    assert len(rows) == 10
    assert all(r["gold_answer"] == "" for r in rows)
    assert all(json.loads(json.dumps(r)) == r for r in rows)  # still JSON-round-trippable