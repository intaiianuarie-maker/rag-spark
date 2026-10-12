"""Automated test for Q8 pinned datasets and Q9 benchmark gate."""

from __future__ import annotations

from evals.run_q9_benchmark import SLICES_FILE, load_slices, run_benchmark


def test_q8_pinned_slices_exist_and_cover_all_surfaces():
    assert SLICES_FILE.exists(), f"Q8 pinned dataset slices file {SLICES_FILE} not found"
    records = load_slices(SLICES_FILE)
    assert len(records) >= 50, f"Expected at least 50 pinned examples, found {len(records)}"

    slices = {r["slice"] for r in records}
    expected = {"en", "ro", "pt", "tables", "multi"}
    assert expected.issubset(slices), f"Missing required slices: {expected - slices}"


def test_q9_benchmark_gate_groundedness_and_citation_validity():
    records = load_slices(SLICES_FILE)
    results = run_benchmark(records)

    overall = results["overall"]
    assert overall["gate_passed"], f"Q9 benchmark gate failed: {overall}"
    assert overall["groundedness"] >= 0.90, (
        f"Groundedness {overall['groundedness']} < 90%"
    )
    assert overall["citation_validity"] == 1.00, (
        f"Citation validity {overall['citation_validity']} != 100%"
    )
