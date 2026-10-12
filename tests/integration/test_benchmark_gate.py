"""Q8/Q9 benchmark gate test — real multilingual slices, hermetic (V1-Q9).

Runs the pinned Q8 slices (EN/RO/PT/tables/multi-source) through the Q9
runner's two independent gates:

- groundedness     >= 0.90  (harness ``score_faithfulness`` vs gold context)
- citation validity == 1.0  (REAL product ``verify_claims`` gate: no
  fabricated/dead citation ever survives)

Hermetic: the gold context is the retrieval oracle (in-memory chunks), so no
Qdrant / Infinity / model gateway is needed. Skips loudly if the pinned slice
file or the datasets are missing (never silently passes).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
for _p in (REPO / "packages", REPO / "apps" / "api", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec and spec.loader, f"{path} must be importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_bq = _load("build_q8_slices", REPO / "evals" / "build_q8_slices.py")
_q9 = _load("q9_run_benchmark", REPO / "evals" / "q9_run_benchmark.py")

SLICES = _bq.SLICES
GROUNDEDNESS_GATE = _q9.GROUNDEDNESS_GATE
CITATION_GATE = _q9.CITATION_GATE
SLICES_FILE = REPO / "evals" / "q8_eval_slices.jsonl"

pytestmark = pytest.mark.integration


def test_q8_pinned_slices_exist_and_cover_all_surfaces() -> None:
    assert SLICES_FILE.exists(), f"Q8 pinned slices file {SLICES_FILE} not found"
    records = _bq.load_slices()
    assert len(records) >= 50, f"expected >= 50 pinned examples, found {len(records)}"
    covered = {r["slice"] for r in records}
    expected = {"en", "ro", "pt", "tables", "multi"}
    missing = expected - covered
    assert not missing, f"missing required Q8 slices: {missing}"
    # Every record is content-pinned and non-empty.
    for r in records:
        assert r["question"].strip() and r["context"].strip(), r["source_id"]
        assert len(r["content_sha256"]) >= 16, r["source_id"]


def test_q9_benchmark_gate_groundedness_and_citation_validity() -> None:
    summary = _q9.run_benchmark()

    overall = summary["_overall"]
    assert overall["groundedness"] >= GROUNDEDNESS_GATE, (
        f"Q9 groundedness {overall['groundedness']:.4f} < {GROUNDEDNESS_GATE}"
    )
    assert overall["citation_validity"] == CITATION_GATE, (
        f"Q9 citation validity {overall['citation_validity']:.4f} != {CITATION_GATE}"
    )
    assert overall["fabricated_survived"] == 0, (
        "a fabricated/dead citation survived verification (false success)"
    )


def test_q9_benchmark_gate_holds_per_slice() -> None:
    summary = _q9.run_benchmark()
    for tag in SLICES:
        s = summary[tag]
        assert s["n"] > 0, f"slice {tag} has no scored records"
        assert s["groundedness"] >= GROUNDEDNESS_GATE, (
            f"{tag} groundedness {s['groundedness']:.4f} < {GROUNDEDNESS_GATE}"
        )
        assert s["citation_validity"] == CITATION_GATE, (
            f"{tag} citation validity {s['citation_validity']:.4f} != {CITATION_GATE}"
        )
