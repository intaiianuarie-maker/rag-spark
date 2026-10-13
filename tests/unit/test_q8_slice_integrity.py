"""T-V1-075 — integrity of the pinned Q8 eval corpus.

Covers the two gaps the T-V1-036 readiness probe found:
  F2  there was no ``unanswerable`` slice at all;
  F3  ``content_sha256`` was derived-from-but-never-compared, so "frozen" was a
       claim rather than an enforced property (and a tampered ``gold_answer``
       was invisible to it, since the fingerprint only covers question+context).

Every mutation test works on a THROWAWAY COPY in tmp_path. The tracked corpus is
never written by this file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _load_module():
    """Import evals/build_q8_slices.py by path (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location(
        "build_q8_slices_t075", _REPO / "evals" / "build_q8_slices.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


b8 = _load_module()

#: sha256 of the first 50 lines of the corpus as it stood before T-V1-075.
#: Frozen here so a future edit to an existing record cannot pass unnoticed.
BASELINE_FIRST_50_SHA256 = "5004ddc2fa3d86a1ff2a7bbcbb29e9e8e65f2e39910d4c8daacd8da8a8b7ddff"


def _corpus_path() -> Path:
    return _REPO / "evals" / "q8_eval_slices.jsonl"


def _records() -> list[dict]:
    raw = _corpus_path().read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


# --- F2: the unanswerables slice exists -------------------------------------


def test_unanswerable_slice_present_with_at_least_eight_records() -> None:
    unans = [r for r in _records() if r["slice"] == "unanswerable"]
    assert len(unans) >= 8, f"expected >=8 unanswerable records, found {len(unans)}"


def test_unanswerable_is_a_registered_slice() -> None:
    """A slice present in the data but absent from SLICES crashes the Q9 runner
    with KeyError, so registration is part of adding the slice."""
    assert "unanswerable" in b8.SLICES
    assert "unanswerable" in b8.PINNED_SLICE_COUNTS
    present = {r["slice"] for r in _records()}
    assert present == set(b8.SLICES), f"data slices {present} != SLICES {set(b8.SLICES)}"


def test_unanswerable_records_carry_every_field_and_no_answer() -> None:
    required = {
        "slice", "source_id", "source_path", "question",
        "context", "gold_answer", "content_sha256",
    }
    unans = [r for r in _records() if r["slice"] == "unanswerable"]
    for rec in unans:
        assert set(rec) == required, f"{rec.get('source_id')} missing {required - set(rec)}"
        # The slice's whole point: no supportable answer exists.
        assert rec["gold_answer"] == "", f"{rec['source_id']} must have an empty gold_answer"
        assert rec["question"].strip() and rec["context"].strip()


def test_every_record_fingerprint_matches_its_own_content() -> None:
    for rec in _records():
        assert rec["content_sha256"] == b8._sha(rec["question"], rec["context"]), rec["source_id"]


def test_module_authored_rows_reproduce_the_file_records() -> None:
    """The module constant exists so build_slices() can regenerate the slice; if
    it drifts from the data, a rebuild silently produces different bytes."""
    by_id = {r[0]: (r[1], r[2]) for r in b8.UNANSWERABLE_ROWS}
    for rec in _records():
        if rec["slice"] == "unanswerable":
            assert rec["source_id"] in by_id, rec["source_id"]
            assert by_id[rec["source_id"]] == (rec["question"], rec["context"]), rec["source_id"]


# --- F3: the pin is enforced -------------------------------------------------


def test_existing_fifty_records_are_byte_identical() -> None:
    """Append-only guarantee: the pre-existing 50 records were not touched."""
    first_50 = "\n".join(_corpus_path().read_text(encoding="utf-8").splitlines()[:50]) + "\n"
    assert hashlib.sha256(first_50.encode()).hexdigest() == BASELINE_FIRST_50_SHA256


def test_load_slices_enforces_and_accepts_the_real_corpus() -> None:
    """The shipped corpus must pass its own check (enforcement is not a no-op)."""
    records = b8.load_slices()
    assert len(records) == sum(b8.PINNED_SLICE_COUNTS.values())


def test_drift_in_any_field_is_detected(tmp_path: Path) -> None:
    """Flip one character in a COPY; the check must fail loudly."""
    tampered = tmp_path / "tampered.jsonl"
    raw = _corpus_path().read_text(encoding="utf-8")
    tampered.write_text(
        raw.replace("Restructuring liabilities", "Restructuring liabilitie", 1),
        encoding="utf-8",
    )
    target_raw = tampered.read_text(encoding="utf-8")
    records = [json.loads(line) for line in target_raw.splitlines() if line.strip()]
    with pytest.raises(b8.SliceIntegrityError, match="not the pinned corpus"):
        b8.verify_pinned_slices(tampered, target_raw, records)


def test_gold_answer_tampering_is_detected(tmp_path: Path) -> None:
    """content_sha256 covers only question+context, so this drift is invisible to
    a per-record fingerprint alone — it is caught by the whole-file pin."""
    rows = _records()
    victim = next(r for r in rows if r["slice"] == "tables")
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    idx = rows.index(victim)
    assert victim["content_sha256"] == b8._sha(victim["question"], victim["context"])  # still valid
    mutated = dict(victim, gold_answer="999999999")
    lines[idx] = json.dumps(mutated, ensure_ascii=False)
    tampered = tmp_path / "gold.jsonl"
    raw = "\n".join(lines) + "\n"
    tampered.write_text(raw, encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    with pytest.raises(b8.SliceIntegrityError):
        b8.verify_pinned_slices(tampered, raw, records)


def test_dropping_a_record_is_detected(tmp_path: Path) -> None:
    rows = [r for r in _records() if r["slice"] != "unanswerable"]
    raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    tampered = tmp_path / "short.jsonl"
    tampered.write_text(raw, encoding="utf-8")
    with pytest.raises(b8.SliceIntegrityError):
        b8.verify_pinned_slices(tampered, raw, rows)


def test_slice_count_drift_is_detected_even_if_the_hash_is_repinned(tmp_path: Path) -> None:
    """Guards the pin against the 're-pin the hash without thinking' failure."""
    rows = _records()
    # A record genuinely dropped, then the hash repinned to match the new bytes —
    # the well-meaning-but-wrong re-pin this guard exists to catch.
    rows = [r for r in rows if r["slice"] != "unanswerable"]
    raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    saved = b8.PINNED_SLICES_SHA256
    try:
        b8.PINNED_SLICES_SHA256 = hashlib.sha256(raw.encode()).hexdigest()
        with pytest.raises(b8.SliceIntegrityError, match="slice counts"):
            b8.verify_pinned_slices(tmp_path / "repinned.jsonl", raw, rows)
    finally:
        b8.PINNED_SLICES_SHA256 = saved


def test_per_record_fingerprint_drift_is_detected(tmp_path: Path) -> None:
    """A hand-edited record whose stored fingerprint was not recomputed."""
    rows = _records()
    raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    saved = b8.PINNED_SLICES_SHA256
    try:
        b8.PINNED_SLICES_SHA256 = hashlib.sha256(raw.encode()).hexdigest()
        broken = dict(rows[0], question=rows[0]["question"] + " (edited)")
        with pytest.raises(b8.SliceIntegrityError, match="content_sha256"):
            b8.verify_pinned_slices(tmp_path / "f.jsonl", raw, [broken, *rows[1:]])
    finally:
        b8.PINNED_SLICES_SHA256 = saved


def test_non_default_path_is_not_pinned(tmp_path: Path) -> None:
    """An explicit alternative corpus is a deliberate caller choice, not drift."""
    alt = tmp_path / "alt.jsonl"
    alt.write_text(
        json.dumps(
            {
                "slice": "en",
                "source_id": "x",
                "source_path": "x",
                "question": "q",
                "context": "c",
                "gold_answer": "a",
                "content_sha256": b8._sha("q", "c"),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert len(b8.load_slices(alt)) == 1