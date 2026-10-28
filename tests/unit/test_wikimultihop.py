"""T-EVAL-002ACQUIRE — 2WikiMultihopQA loader + evidence-path audit tests.

Pins the corpus (revision, per-file sha256, split row counts) and the audit
numbers measured by ``evals.wikimultihop.audit_evidence_paths``:

  validation  n=12 576   30 687 evidence paths, 0 unresolvable
  train       n=167 454 404 884 evidence paths, 0 unresolvable
  test        n=12 576   unlabeled upstream (no answers, no supporting facts)

The zero is *measured* by the audit pass and the counter is proven live against
a synthetic record, so it cannot be zero by fiat. The full train audit is
opt-in (``WIKIMULTIHOP_FULL_AUDIT=1``) because it reads 330 MB; its counts are
pinned here and in the provenance doc.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import pathlib
import re

import pytest

from evals import wikimultihop as w2w

requires_dataset = pytest.mark.skipif(
    not w2w.WIKIMULTIHOP_ROOT.exists(),
    reason="needs the 2WikiMultiHopQA corpus (not in the repo)",
)

# --------------------------------------------------------------------------- #
# Corpus pins
# --------------------------------------------------------------------------- #


@requires_dataset
@pytest.mark.unit
def test_pinned_files_present_and_sha256_match() -> None:
    verified = w2w.verify_pins()
    assert set(verified) == {
        "train-00000-of-00002.parquet",
        "train-00001-of-00002.parquet",
        "validation-00000-of-00001.parquet",
        "test-00000-of-00001.parquet",
    }
    # Every pin equals the hash published by the hub as the LFS object id.
    for filename, digest in verified.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest), filename
        assert digest in w2w.FILE_SHA256[w2w.split_of(filename)]


@pytest.mark.unit
def test_missing_or_drifted_file_fails_loudly(tmp_path: pathlib.Path) -> None:
    swapped = tmp_path / "validation-00000-of-00001.parquet"
    swapped.write_bytes(b"not the corpus")
    original = w2w.RAW_ROOT
    try:
        w2w.RAW_ROOT = tmp_path
        with pytest.raises(w2w.CorpusPinError):
            w2w.verify_pins("validation")
    finally:
        w2w.RAW_ROOT = original


@requires_dataset
@pytest.mark.unit
def test_split_row_counts_pinned() -> None:
    assert w2w.SPLIT_ROWS == {"train": 167_454, "validation": 12_576, "test": 12_576}
    for split, expected in w2w.SPLIT_ROWS.items():
        assert w2w.row_counts(split) == expected


# --------------------------------------------------------------------------- #
# Evidence-path audit — the counted numbers
# --------------------------------------------------------------------------- #

VALIDATION_AUDIT = {
    "n_records": 12_576,
    "n_records_with_supporting_facts": 12_576,
    "n_supporting_facts": 30_687,
    "n_resolvable": 30_687,
    "n_unresolvable": 0,
    "n_title_not_in_context": 0,
    "n_sent_id_out_of_range": 0,
    "n_records_unresolvable": 0,
    "n_records_duplicate_title": 463,
    "n_duplicate_title_groups": 523,
    "n_duplicate_title_groups_conflicting": 0,
    "n_records_missing_field": 0,
    "n_records_without_answer": 0,
}

TRAIN_AUDIT = {
    "n_records": 167_454,
    "n_supporting_facts": 404_884,
    "n_unresolvable": 0,
    "n_duplicate_title_groups": 11_865,
    "n_duplicate_title_groups_conflicting": 0,
    "n_records_missing_field": 0,
}


@requires_dataset
@pytest.mark.unit
def test_validation_audit_counts_pinned() -> None:
    audit = w2w.audit_evidence_paths("validation")
    measured = dataclasses.asdict(audit)
    measured.pop("split")
    assert measured == VALIDATION_AUDIT
    # Internal consistency: every path is either resolvable or unresolvable.
    assert audit.n_resolvable + audit.n_unresolvable == audit.n_supporting_facts
    assert audit.unresolvable == audit.n_title_not_in_context + audit.n_sent_id_out_of_range


@requires_dataset
@pytest.mark.unit
def test_test_split_is_unlabeled_upstream() -> None:
    audit = w2w.audit_evidence_paths("test")
    assert audit.n_records == 12_576
    assert audit.n_supporting_facts == 0
    assert audit.n_records_without_answer == 12_576 == audit.n_records
    assert audit.n_records_duplicate_title == 439
    assert audit.n_duplicate_title_groups_conflicting == 0


@pytest.mark.unit
def test_train_audit_counts_pinned() -> None:
    if os.environ.get("WIKIMULTIHOP_FULL_AUDIT") != "1":
        pytest.skip("full train audit reads 330 MB; set WIKIMULTIHOP_FULL_AUDIT=1 to run")
    measured = dataclasses.asdict(w2w.audit_evidence_paths("train"))
    measured.pop("split")
    for key, want in TRAIN_AUDIT.items():
        assert measured[key] == want, key


@pytest.mark.unit
def test_unresolvable_counter_is_live_not_hardcoded_zero() -> None:
    """A record with a bogus path must register as unresolvable.

    This is what makes the measured 0 above meaningful: the counter demonstrably
    counts rather than returning a constant.
    """
    record = w2w.WikiMultiHopRecord(
        id="synthetic",
        question="q",
        answer="a",
        type="compositional",
        evidences=(),
        supporting_facts=(
            w2w.SupportingFact("Present Title", 0),
            w2w.SupportingFact("Absent Title", 0),
            w2w.SupportingFact("Present Title", 99),
        ),
        context={"Present Title": ("only sentence",)},
        split="synthetic",
    )
    unresolved = record.unresolved()
    assert [r.reason for r in unresolved] == [
        "title_not_in_context",
        "sent_id_out_of_range",
    ]
    assert all(not r.resolved for r in unresolved)
    # The resolvable one still yields its text.
    assert record.supporting_text() == ("only sentence",)


@requires_dataset
@pytest.mark.unit
def test_duplicate_context_titles_are_benign() -> None:
    """Repeats exist but never disagree, so sent_id is never ambiguous."""
    groups, conflicting = w2w.audit_duplicate_context_titles("validation")
    assert (groups, conflicting) == (523, 0)
    assert w2w.audit_evidence_paths("validation").n_duplicate_title_groups_conflicting == 0


# --------------------------------------------------------------------------- #
# Loader surface
# --------------------------------------------------------------------------- #


@requires_dataset
@pytest.mark.unit
def test_records_carry_every_required_field() -> None:
    records = w2w.load_records("validation", limit=200)
    assert len(records) == 200
    for record in records:
        assert record.missing_fields == ()
        assert record.id and record.question and record.answer and record.type
        assert record.context and record.supporting_facts
        assert record.resolved_paths() == record.supporting_facts


@requires_dataset
@pytest.mark.unit
def test_streaming_respects_limit_and_never_materializes_train() -> None:
    """train is 167k records; the streaming path must stop at the limit."""
    records = list(w2w.iter_records("train", limit=25))
    assert len(records) == 25
    assert all(r.split == "train" for r in records)


@pytest.mark.unit
def test_unknown_split_is_refused() -> None:
    with pytest.raises(KeyError):
        list(w2w.iter_records("dev"))


@pytest.mark.unit
def test_loader_writes_no_scoring_code() -> None:
    """scoring code written: 0 — the loader imports no metric, no embedder.

    Official 2Wiki EM/F1 stay placeholders in the T-EVAL-007 harness; a test
    here keeps this module from quietly growing its own.
    """
    forbidden = ("f1", "_em", "metric", "score", "embed", "rerank", "retrieve")
    leaked = [
        name for name in w2w.__all__ if any(token in name.lower() for token in forbidden)
    ]
    assert leaked == []
    # Import surface is the stdlib plus pyarrow — nothing that scores or embeds.
    imported: set[str] = set()
    for node in ast.walk(ast.parse(pathlib.Path(w2w.__file__).read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__",
        "dataclasses",
        "hashlib",
        "pathlib",
        "collections",
        "pyarrow",
        "evals",
    }


@requires_dataset
@pytest.mark.unit
def test_official_2wiki_metrics_remain_pending_in_the_harness() -> None:
    """The loader feeds the T-EVAL-007 interfaces; it does not satisfy them."""
    from evals import leaderboard as lb

    assert lb.official_pending("twowiki_answer_em") is True
    assert lb.official_pending("twowiki_answer_f1") is True
    assert lb.MAPPING["twowiki_answer_em"] == lb.OFFICIAL
    # Records expose exactly what the pending contract needs: an answer string
    # per item.
    record = next(w2w.iter_records("validation"))
    assert isinstance(record.answer, str) and record.answer


# --------------------------------------------------------------------------- #
# Provenance record
# --------------------------------------------------------------------------- #


@requires_dataset
@pytest.mark.unit
def test_provenance_doc_has_no_missing_fields() -> None:
    """provenance fields missing: 0 — every required provenance fact recorded."""
    doc = w2w.PROVENANCE_PATH.read_text()
    lowered = doc.lower()
    for needle in (
        w2w.DATASET_REPO,
        w2w.DATASET_REVISION,
        w2w.DATASET_LICENSE,
        w2w.UPSTREAM_REPO,
        "Apache License 2.0",
        "internal evaluation use only",
        "Never ingested as live knowledge",
    ):
        assert needle.lower() in lowered, needle
    for files in w2w.FILE_SHA256.values():
        for digest in files:
            assert digest in doc, digest
    # The measured audit numbers travel with the data.
    for number in (
        "12 576",
        "30 687",
        "404 884",
        "435 571",
        "192 606",
        "12 881",
        "10 485",
    ):
        assert number in doc, number
    assert "Unresolvable evidence paths: 0" in doc


@requires_dataset
@pytest.mark.unit
def test_license_and_card_files_sit_next_to_the_data() -> None:
    assert w2w.LICENSE_PATH.exists()
    assert "Apache License" in w2w.LICENSE_PATH.read_text()[:200]
    assert w2w.CARD_PATH.exists()
    card = w2w.CARD_PATH.read_text().replace("\u00a0", " ")
    assert "Apache License 2.0" in card  # NBSP in the card's license sentence
    metadata = w2w.HUB_METADATA_PATH.read_text()
    assert "framolfese/2WikiMultihopQA" in metadata
    # Recorded, not waived: the hub itself declares no license.
    assert '"license"' not in metadata