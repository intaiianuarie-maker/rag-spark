"""T-EVAL-003ACQUIRE — MuSiQue loader + evidence-path audit tests.

Pins the corpus (revision, per-file sha256, record counts) and the audit
numbers measured by ``evals.musique.audit_evidence_paths``:

  ans   2 417 records, all answerable; 6 404 evidence paths, 0 unresolvable
  full  4 834 records (2 417 answerable + 2 417 unanswerable); 12 808 hops,
        9 133 evidence paths, 0 unresolvable

The zero is *measured* by the audit pass and proven live against a synthetic
record, so it cannot be zero by fiat. Scoring / retrieval / embedding live in
the T-EVAL-007 harness, not here.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import re

import pytest

from evals import musique as muz

requires_dataset = pytest.mark.skipif(
    not muz.MUSIQUE_ROOT.exists(),
    reason="needs the MuSiQue corpus under evals.musique.MUSIQUE_ROOT (not in the repo)",
)

# --------------------------------------------------------------------------- #
# Corpus pins
# --------------------------------------------------------------------------- #


@requires_dataset
@pytest.mark.unit
def test_pinned_files_present_and_sha256_match() -> None:
    for config in muz.FILE_SHA256:
        verified = muz.verify_pins(config)
        assert set(verified) == set(muz.FILE_SHA256[config])
        for filename, digest in verified.items():
            assert re.fullmatch(r"[0-9a-f]{64}", digest), filename
            assert digest == muz.FILE_SHA256[config][filename]


@pytest.mark.unit
def test_missing_or_drifted_file_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A swapped or absent file must raise CorpusPinError, never silently load."""
    # Missing file: an empty tmp root has the expected file absent.
    monkeypatch.setattr(muz, "RAW_ROOT", tmp_path)
    with pytest.raises(muz.CorpusPinError, match="missing pinned file"):
        muz.verify_pins("ans")

    # Drifted content: wrong bytes under the expected filename.
    (tmp_path / "musique_ans_v1.0_dev.jsonl").write_bytes(b"not the corpus")
    with pytest.raises(muz.CorpusPinError, match="sha256"):
        muz.verify_pins("ans")


# --------------------------------------------------------------------------- #
# Row counts + audit (the measured numbers)
# --------------------------------------------------------------------------- #


ANS_AUDIT = {
    "n_records": 2_417,
    "n_answerable": 2_417,
    "n_unanswerable": 0,
    "n_hops": 6_404,
    "n_records_with_decomposition": 2_417,
    "hop_count_histogram": {2: 1_252, 3: 760, 4: 405},
    "supporting_paragraph_count_histogram": {2: 1_252, 3: 760, 4: 405},
    "paragraph_count_histogram": {17: 2, 18: 5, 19: 9, 20: 2_401},
    "n_evidence_paths": 6_404,
    "n_hops_null_idx": 0,
    "n_resolvable": 6_404,
    "n_unresolvable": 0,
    "n_idx_out_of_range": 0,
    "n_records_missing_field": 0,
    "n_records_missing_decomposition": 0,
}

FULL_AUDIT = {
    "n_records": 4_834,
    "n_answerable": 2_417,
    "n_unanswerable": 2_417,
    "n_hops": 12_808,
    "n_records_with_decomposition": 4_834,
    "hop_count_histogram": {2: 2_504, 3: 1_520, 4: 810},
    "supporting_paragraph_count_histogram": {0: 541, 1: 1_178, 2: 1_795, 3: 915, 4: 405},
    "paragraph_count_histogram": {15: 1, 16: 2, 17: 2, 18: 9, 19: 17, 20: 4_803},
    "n_evidence_paths": 9_133,
    "n_hops_null_idx": 3_675,
    "n_resolvable": 9_133,
    "n_unresolvable": 0,
    "n_idx_out_of_range": 0,
    "n_records_missing_field": 0,
    "n_records_missing_decomposition": 0,
}


@requires_dataset
@pytest.mark.unit
def test_row_counts_and_audit_counts() -> None:
    # ans: 2 417 records, all answerable.
    ans = muz.audit_evidence_paths("ans")
    assert ans.n_records == 2_417
    assert ans.n_answerable == 2_417
    assert ans.n_unanswerable == 0
    measured = dataclasses.asdict(ans)
    measured.pop("config")
    assert measured == ANS_AUDIT
    # Internal consistency: every evidence path is either resolvable or not.
    assert ans.n_resolvable + ans.n_unresolvable == ans.n_evidence_paths

    # full: 4 834 records, 2 417 answerable + 2 417 unanswerable.
    full = muz.audit_evidence_paths("full")
    assert full.n_records == 4_834
    assert full.n_answerable == 2_417
    assert full.n_unanswerable == 2_417
    measured_full = dataclasses.asdict(full)
    measured_full.pop("config")
    assert measured_full == FULL_AUDIT
    assert full.n_resolvable + full.n_unresolvable == full.n_evidence_paths


# --------------------------------------------------------------------------- #
# Record shape / typing
# --------------------------------------------------------------------------- #


@requires_dataset
@pytest.mark.unit
def test_record_fields_typed() -> None:
    record = next(muz.iter_records("ans", verify=False))
    assert isinstance(record, muz.MuSiQueRecord)
    assert dataclasses.is_dataclass(record)
    # frozen: assignment must be refused at runtime (retrieval cannot backfill).
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.answer = "x"  # type: ignore[misc]
    # scalar fields
    assert isinstance(record.id, str) and record.id
    assert isinstance(record.question, str) and record.question
    assert isinstance(record.answer, str) and record.answer
    assert isinstance(record.answerable, bool)
    assert record.config == "ans"
    assert record.missing_fields == ()
    # paragraphs
    assert isinstance(record.paragraphs, tuple) and record.paragraphs
    assert record.n_paragraphs == len(record.paragraphs)
    p0 = record.paragraphs[0]
    assert isinstance(p0, muz.MuSiQueParagraph)
    assert isinstance(p0.idx, int)
    assert isinstance(p0.title, str)
    assert isinstance(p0.paragraph_text, str)
    assert isinstance(p0.is_supporting, bool)
    # decomposition hops
    assert isinstance(record.question_decomposition, tuple)
    assert record.question_decomposition
    hop = record.question_decomposition[0]
    assert isinstance(hop, muz.MuSiQueDecompositionHop)
    assert isinstance(hop.id, int)
    assert isinstance(hop.question, str)
    assert hop.paragraph_support_idx is None or isinstance(
        hop.paragraph_support_idx, int
    )
    assert isinstance(record.answer_aliases, tuple)
    # ans is fully answerable: every hop carries a supporting-paragraph index.
    assert record.answerable
    assert all(
        h.paragraph_support_idx is not None
        for h in record.question_decomposition
    )


# --------------------------------------------------------------------------- #
# Acquisition boundary: no scoring / retrieval / embedding / ingestion
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_loader_writes_no_scoring_or_retrieval_code() -> None:
    """No scoring/retrieval code in the loader: 0 unexpected imports."""
    tree = ast.parse(pathlib.Path(muz.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__",
        "dataclasses",
        "hashlib",
        "json",
        "pathlib",
        "collections",
        "typing",
    }, f"unexpected imports: {imported}"
    forbidden = ("f1", "_em", "metric", "score", "embed", "rerank", "retrieve")
    leaked = [name for name in muz.__all__ if any(t in name.lower() for t in forbidden)]
    assert leaked == []


@pytest.mark.unit
def test_unresolvable_counter_is_live_not_hardcoded_zero() -> None:
    """Records with broken evidence paths must register as unresolvable."""
    record = muz.MuSiQueRecord(
        id="synthetic",
        paragraphs=(
            muz.MuSiQueParagraph(
                idx=0, title="Gold", paragraph_text="yes", is_supporting=True
            ),
            muz.MuSiQueParagraph(
                idx=1, title="Distractor", paragraph_text="no", is_supporting=False
            ),
        ),
        question="q",
        question_decomposition=(
            muz.MuSiQueDecompositionHop(
                id=1, question="h1", answer="a", paragraph_support_idx=0
            ),
            muz.MuSiQueDecompositionHop(
                id=2, question="h2", answer="b", paragraph_support_idx=1
            ),
            muz.MuSiQueDecompositionHop(
                id=3, question="h3", answer="c", paragraph_support_idx=5
            ),
            muz.MuSiQueDecompositionHop(
                id=4, question="h4", answer="d", paragraph_support_idx=None
            ),
        ),
        answer="a",
        answer_aliases=(),
        answerable=True,
        config="ans",
    )
    resolutions = [record.resolve(h) for h in record.question_decomposition]
    assert [r.resolved for r in resolutions] == [True, False, False, False]
    assert resolutions[1].reason == "not_supporting"
    assert resolutions[2].reason == "idx_out_of_range"
    assert resolutions[3].reason == "null_support_idx"
    unresolved = record.unresolved()
    # Only hops that DECLARE an index but fail resolve (a null index is not a path).
    assert len(unresolved) == 2
    # The resolvable path still yields its supporting text.
    assert record.supporting_evidence == ("yes",)


# --------------------------------------------------------------------------- #
# Provenance record
# --------------------------------------------------------------------------- #


@requires_dataset
@pytest.mark.unit
def test_provenance_doc_records_license_hashes_and_audits() -> None:
    doc = muz.PROVENANCE_PATH.read_text(encoding="utf-8")
    lowered = doc.lower()
    for needle in (
        muz.DATASET_REPO,
        muz.DATASET_REVISION,
        muz.DATASET_LICENSE,
        muz.UPSTREAM_REPO,
        muz.PAPER,
        "CC BY 4.0",
        "internal evaluation use only",
        "Never ingested as live knowledge",
        "Unresolvable evidence paths: 0",
    ):
        assert needle.lower() in lowered, needle
    for digest in (
        "15fa63794d18a94ce12411aca6e2327e65b6e83b0b1490efab3f1962e48abf3b",
        "8cab31d56a3a1c4ef491b205a8dab3f1ac9c66e472098c6cf1de4e20294f7a4a",
    ):
        assert digest in doc, digest
    for number in ("2 417", "4 834", "6 404", "9 133", "12 808"):
        assert number in doc, number


@requires_dataset
@pytest.mark.unit
def test_every_declared_file_present_and_no_strays() -> None:
    """A stray JSONL in the dataset dir would mean unacquired, unverified data."""
    declared = {
        muz.RAW_ROOT / filename
        for config in muz.FILE_SHA256
        for filename in muz.FILE_SHA256[config]
    }
    found = set(muz.RAW_ROOT.glob("*.jsonl"))
    assert found == declared, f"undeclared/absent files: {found ^ declared}"
