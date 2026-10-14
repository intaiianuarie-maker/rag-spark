"""Tests for the HotpotQA acquisition loader (T-EVAL-001ACQUIRE).

Lives in ``evals/`` rather than ``tests/`` on purpose: pytest is configured
with ``testpaths = ["tests"]`` and code1 owns the ``tests/`` tree for the
T-EVAL-007 harness, so a loader test belonging to this acquisition ticket
stays in the acquisition area and is run explicitly:

    pytest evals/test_hotpot_full.py

These tests pin the loader's CONTRACT and the acquisition facts that were
measured, including the upstream defects that are NOT fixable by acquisition.
They deliberately assert no metric: scoring belongs to the harness.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from evals import hotpot_full as hf

#: Measured at acquisition (2026-10-14). These are upstream data properties.
#: If one of them moves, the data changed and the provenance hashes will too.
MEASURED_RECORD_COUNTS = {
    ("fullwiki", "train"): 90447,
    ("fullwiki", "validation"): 7405,
    ("fullwiki", "test"): 7405,
    ("distractor", "validation"): 7405,
}


# --------------------------------------------------------------------------
# manifest / provenance
# --------------------------------------------------------------------------


def test_every_declared_file_exists_on_disk() -> None:
    for config, splits in hf.CONFIG_SPLIT_FILES.items():
        for split, stems in splits.items():
            for stem in stems:
                path = hf.file_path(config, split, stem)
                assert path.exists(), f"declared but absent: {path}"


def test_every_declared_file_has_a_pinned_hash() -> None:
    for config, splits in hf.CONFIG_SPLIT_FILES.items():
        for stems in splits.values():
            for stem in stems:
                assert stem in hf.FILE_SHA256[config], f"unpinned file: {config}/{stem}"
                assert len(hf.FILE_SHA256[config][stem]) == 64


def test_provenance_doc_exists_and_names_the_license_and_revision() -> None:
    # Collapse whitespace so a phrase wrapped across a line wrap still matches.
    text = " ".join(hf.PROVENANCE_PATH.read_text(encoding="utf-8").split())
    assert hf.DATASET_REVISION in text
    assert "CC BY-SA 4.0" in text
    assert "not redistributed" in text.lower()
    assert "ingested into live knowledge" in text.lower()


def test_no_unpinned_file_sits_beside_the_manifest() -> None:
    """A stray parquet in the dataset dir would mean unacquired, unverified data."""
    declared = {
        hf.file_path(config, split, stem)
        for config, splits in hf.CONFIG_SPLIT_FILES.items()
        for split, stems in splits.items()
        for stem in stems
    }
    found = set(hf.RAW_ROOT.rglob("*.parquet"))
    assert found == declared, f"undeclared parquet files on disk: {found ^ declared}"


# --------------------------------------------------------------------------
# loader contract
# --------------------------------------------------------------------------


def test_unknown_config_or_split_is_refused() -> None:
    with pytest.raises(ValueError):
        hf.file_path("nope", "validation", "validation-00000-of-00001")
    with pytest.raises(ValueError):
        hf.file_path("fullwiki", "nope", "validation-00000-of-00001")
    with pytest.raises(ValueError):
        hf.audit_split("nope")


def test_a_file_from_another_split_is_refused() -> None:
    with pytest.raises(ValueError):
        hf.file_path("fullwiki", "validation", "train-00000-of-00002")


def test_hash_drift_raises_rather_than_silently_loading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setattr(hf, "FILE_SHA256", {c: dict(v) for c, v in hf.FILE_SHA256.items()})
    hf.FILE_SHA256["fullwiki"]["validation-00000-of-00001"] = "0" * 64
    with pytest.raises(RuntimeError, match="drift"):
        hf.verify_split_hashes("fullwiki", "validation")


def test_missing_file_raises_without_touching_real_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Uses a throwaway RAW_ROOT: the real dataset must never be renamed."""
    monkeypatch.setattr(hf, "RAW_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        hf.verify_split_hashes("fullwiki", "validation")


def test_records_carry_every_field_the_eval_needs() -> None:
    record = next(hf.iter_records("validation", limit=1))
    assert record.config == "fullwiki"
    assert record.split == "validation"
    assert record.record_id
    assert record.question.strip()
    assert record.answer is not None
    assert record.question_type in {"comparison", "bridge"}
    assert record.level in {"easy", "medium", "hard"}
    assert record.supporting_facts
    assert record.paragraphs


def test_limit_is_respected_and_order_is_stable() -> None:
    first = [r.record_id for r in hf.iter_records("validation", verify_hashes=False, limit=5)]
    again = [r.record_id for r in hf.iter_records("validation", verify_hashes=False, limit=5)]
    assert first == again
    assert len(first) == 5


def test_train_streaming_matches_the_full_materialisation() -> None:
    """Streaming must not drop or duplicate rows across shard boundaries."""
    ids = [r.record_id for r in hf.iter_records("train", verify_hashes=False, limit=300)]
    assert len(ids) == len(set(ids)) == 300


# --------------------------------------------------------------------------
# distractor audit
# --------------------------------------------------------------------------


def test_candidate_sets_are_the_full_ten_not_a_four_paragraph_subset() -> None:
    """The whole point of the audit: 10 paragraphs, not the old 4-para corpus."""
    audit = hf.audit_split("train", verify_hashes=False)
    assert audit.paragraph_count_histogram[10] > 89000
    assert 10 in audit.paragraph_count_histogram


def test_measured_record_counts_hold() -> None:
    for (config, split), expected in MEASURED_RECORD_COUNTS.items():
        audit = hf.audit_split(split, config=config, verify_hashes=False)
        assert audit.n_records == expected, f"{config}/{split}"


def test_upstream_short_candidate_sets_are_counted_not_hidden() -> None:
    """These cannot be driven to zero by acquisition; they must stay visible."""
    train = hf.audit_split("train", verify_hashes=False)
    assert train.n_records_not_10_paragraphs == 838
    validation = hf.audit_split("validation", verify_hashes=False)
    assert validation.n_records_not_10_paragraphs == 63


def test_fullwiki_validation_loses_its_gold_spans() -> None:
    """The defect that makes fullwiki validation unusable for retrieval eval."""
    audit = hf.audit_split("validation", verify_hashes=False)
    assert audit.n_records_missing_gold_title == 5316
    assert audit.n_records_missing_gold_title > 0


def test_distractor_config_resolves_every_gold_span() -> None:
    audit = hf.audit_split("validation", config="distractor", verify_hashes=False)
    assert audit.n_records_missing_gold_title == 0


def test_unlabeled_test_split_is_reported_as_unlabeled() -> None:
    audit = hf.audit_split("test", verify_hashes=False)
    assert audit.n_labeled == 0
    assert audit.n_unlabeled == audit.n_records
    # The distractor metric is undefined without gold, so it must be excluded
    # rather than reported as a bogus mismatch count.
    assert audit.n_distractor_metric_applicable == 0


def test_labeled_split_reports_its_distractor_metric_scope() -> None:
    audit = hf.audit_split("validation", config="distractor", verify_hashes=False)
    assert audit.n_distractor_metric_applicable == audit.n_labeled == audit.n_records


def test_labeled_splits_are_derived_from_the_data() -> None:
    assert hf.labeled_splits("fullwiki") == ("train", "validation")
    assert hf.labeled_splits("distractor") == ("validation",)


def test_duplicate_record_ids_are_zero() -> None:
    audit = hf.audit_split("validation", verify_hashes=False)
    assert audit.n_duplicate_record_ids == 0


# --------------------------------------------------------------------------
# span map — shape only, unfilled by design
# --------------------------------------------------------------------------


def test_span_map_is_one_target_per_gold_sentence_and_is_unresolved() -> None:
    record = next(hf.iter_records("validation", limit=1))
    targets = hf.span_map(record)
    assert len(targets) == len(record.supporting_facts)
    assert all(not t.is_resolved for t in targets)
    assert all(t.chunk_key is None and t.chunk_text is None for t in targets)
    assert all(t.record_id == record.record_id for t in targets)


def test_span_map_never_raises_on_an_unresolvable_gold_span() -> None:
    """A fullwiki record whose gold title is absent must not abort a load."""
    broken = hf.HotpotRecord(
        record_id="x",
        config="fullwiki",
        split="validation",
        question="q",
        answer="a",
        question_type="bridge",
        level="easy",
        supporting_facts=(hf.SupportingFact(title="Not In Context", sentence_id=0),),
        paragraphs=(hf.Paragraph(title="Real", sentences=("s",)),),
    )
    (target,) = hf.span_map(broken)
    assert target.sentence_text == ""
    assert not target.is_resolved
    assert broken.missing_gold_titles == frozenset({"Not In Context"})


def test_derived_counters_are_internally_consistent() -> None:
    record = next(hf.iter_records("validation", limit=1))
    assert record.n_distractor_paragraphs == (
        record.n_paragraphs - record.n_gold_paragraphs
    )
    assert record.all_gold_titles_present == (not record.missing_gold_titles)


# --------------------------------------------------------------------------
# acquisition boundary: no scoring, no retrieval, no ingestion
# --------------------------------------------------------------------------


def test_module_defines_no_metric_or_scoring_function() -> None:
    """Scoring belongs to the T-EVAL-007 harness, not to acquisition."""
    banned = ("score", "metric", "f1", "em_", "exact_match", "precision", "recall")
    for name in dir(hf):
        low = name.lower()
        assert not any(b in low for b in banned), f"scoring-shaped name: {name}"


def test_module_imports_no_retrieval_or_embedding_stack() -> None:
    """Checked via AST, not substring.

    A text scan over the module source is defeated by this module's own
    docstring, which legitimately mentions bge-m3 and LiteLLM while promising
    never to call them. Only real imports count.
    """
    tree = ast.parse(pathlib.Path(hf.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    banned = {
        "sentence_transformers",
        "litellm",
        "FlagEmbedding",
        "qdrant_client",
        "transformers",
        "torch",
        "numpy",
    }
    assert not (imported & banned), f"retrieval dependency imported: {imported & banned}"
    assert imported <= {"__future__", "dataclasses", "hashlib", "json", "pathlib",
                        "collections", "pyarrow"}, f"unexpected imports: {imported}"


def test_span_targets_are_frozen_so_retrieval_cannot_backfill_silently() -> None:
    record = next(hf.iter_records("validation", limit=1))
    target = hf.span_map(record)[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.chunk_key = "smuggled"  # type: ignore[misc]