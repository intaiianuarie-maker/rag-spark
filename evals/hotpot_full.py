"""HotpotQA raw record loader + distractor audit.

T-EVAL-001ACQUIRE. ACQUISITION AND RAW LOADING ONLY.

This module stops exactly where scoring begins. It converts the on-disk
parquet into typed records (question, answer, supporting facts, the distractor
candidate set) and audits that candidate set. It deliberately does NOT
retrieve, embed, rank or score anything:

  * no bge-m3 / any embedder call — real bge-m3 runs when retrieval runs;
  * no metric function of any kind — metrics belong to the T-EVAL-007 harness
    that dev1 owns;
  * no ingestion — this dataset is scratch evaluation material, never live
    knowledge (see LICENSE-AND-PROVENANCE.md).

``SpanTarget`` is the SHAPE of the answer-span -> chunk mapping, with the
retrieval-owned half left unset. Filling those fields is a later ticket.

Two configs are acquired, and the difference is NOT cosmetic:

  ``fullwiki``    the 10-paragraph candidate set comes from Wikipedia full-text
                  search, so the gold article is frequently absent or
                  disambiguated differently ("Ed Wood" gold vs "Ed Wood (film)"
                  candidate). On validation, 5316/7405 records have a
                  supporting-fact title that is not in the candidate set at all.
  ``distractor``  the candidate set is the one HotpotQA published with the gold
                  spans, so every gold title resolves (0 missing on validation).

Provenance: every file is pinned to dataset revision 1908d6af and verified by
sha256 on load against :data:`FILE_SHA256`, so a drifted or swapped file fails
loudly instead of silently changing eval numbers (the pattern already used by
``evals/g5_corpus.py``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
from collections.abc import Iterator

import pyarrow.parquet as pq

#: On-disk location of the acquired dataset.
HOTPOT_ROOT = pathlib.Path("/root/code/Super Rag/datasets/hotpotqa-full")
#: Directory holding the pinned parquet files, one sub-directory per config.
RAW_ROOT = HOTPOT_ROOT / "raw"
#: License + provenance record that must travel with the data.
PROVENANCE_PATH = HOTPOT_ROOT / "LICENSE-AND-PROVENANCE.md"

#: Hugging Face dataset coordinates the files were pulled from.
DATASET_REPO = "hotpotqa/hotpot_qa"
DATASET_REVISION = "1908d6afbbead072334abe2965f91bd2709910ab"
DATASET_LICENSE = "cc-by-sa-4.0"

#: Config loaded when a caller does not name one.
DEFAULT_CONFIG = "fullwiki"

#: Parquet files per (config, split), in a fixed order. Split names are the
#: Hugging Face ones.
CONFIG_SPLIT_FILES: dict[str, dict[str, tuple[str, ...]]] = {
    "fullwiki": {
        "train": ("train-00000-of-00002", "train-00001-of-00002"),
        "validation": ("validation-00000-of-00001",),
        # Unlabeled HotpotQA test set: answer=None, supporting_facts empty.
        "test": ("test-00000-of-00001",),
    },
    "distractor": {
        "validation": ("validation-00000-of-00001",),
    },
}

#: sha256 of each file at :data:`DATASET_REVISION`. These are the LFS object ids
#: published by the hub, i.e. the upstream hashes rather than merely our own
#: record of whatever happened to arrive on disk.
FILE_SHA256: dict[str, dict[str, str]] = {
    "fullwiki": {
        "train-00000-of-00002":
            "76d3bb3048a7cc73c1958107c0c5872a00d7e7d00c105b81e92f6769e7822e68",
        "train-00001-of-00002":
            "713661628434fbb19fff7392e2e321e4ed107e3c7c7784d0690946e5f722763f",
        "validation-00000-of-00001":
            "78933c0a31a5f7b420d4effdf4cd4eed573b28c6a3da6179dcf7a02b39e51d03",
        "test-00000-of-00001":
            "f6b522fed6748b33c0eaad972d53bc89c10b8afefb329629f779bfb967442cd8",
    },
    "distractor": {
        "validation-00000-of-00001":
            "c20b638ca82b21d04fe12e14ff417ad05153d4d215a65de54497fca4e972f7c6",
    },
}

#: Splits acquired per config, for convenience.
CONFIG_SPLITS: dict[str, tuple[str, ...]] = {
    config: tuple(splits) for config, splits in CONFIG_SPLIT_FILES.items()
}

#: A complete HotpotQA candidate set is 10 paragraphs: 2 gold supporting
#: paragraphs plus 8 distractors.
EXPECTED_PARAGRAPHS_PER_RECORD = 10
EXPECTED_DISTRACTOR_PARAGRAPHS_PER_RECORD = 8

#: Batch size for parquet reads. Large enough to be quick, small enough that a
#: 90k-row split is never materialised by a caller that only iterates.
_BATCH_ROWS = 512


@dataclasses.dataclass(frozen=True, slots=True)
class Paragraph:
    """One candidate paragraph from the distractor set."""

    title: str
    sentences: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join(self.sentences)


@dataclasses.dataclass(frozen=True, slots=True)
class SupportingFact:
    """A gold (title, sentence_id) pair as published by the dataset."""

    title: str
    sentence_id: int


@dataclasses.dataclass(frozen=True, slots=True)
class HotpotRecord:
    """One raw HotpotQA record. No retrieval, no chunking, no scoring."""

    record_id: str
    config: str
    split: str
    question: str
    answer: str | None
    question_type: str | None
    level: str | None
    supporting_facts: tuple[SupportingFact, ...]
    paragraphs: tuple[Paragraph, ...]
    #: True when the source title list and sentence-list column disagreed in
    #: length, so the candidate set may have been truncated by the loader.
    context_ragged: bool = False

    @property
    def is_labeled(self) -> bool:
        """A record is labeled iff it carries an answer and supporting facts.

        The ``fullwiki`` ``test`` split is the unlabeled HotpotQA test set:
        7405 rows with ``answer=None`` and empty ``supporting_facts``. It is
        acquired for completeness but cannot be scored, and the audit reports
        labeled/unlabeled counts separately so no harness can quietly score
        empty gold.
        """
        return bool(self.answer) and bool(self.supporting_facts)

    @property
    def n_paragraphs(self) -> int:
        return len(self.paragraphs)

    @property
    def gold_titles(self) -> frozenset[str]:
        return frozenset(fact.title for fact in self.supporting_facts)

    @property
    def n_gold_paragraphs(self) -> int:
        """Gold paragraphs actually present in the candidate set.

        Presence is counted rather than assumed: a supporting-fact title absent
        from the candidate set is exactly the ``fullwiki`` failure mode.
        """
        titles = {p.title for p in self.paragraphs}
        return len(self.gold_titles & titles)

    @property
    def n_distractor_paragraphs(self) -> int:
        """Candidate paragraphs that are not gold supporting paragraphs."""
        return self.n_paragraphs - self.n_gold_paragraphs

    @property
    def missing_gold_titles(self) -> frozenset[str]:
        """Gold titles the candidate set does not contain."""
        titles = {p.title for p in self.paragraphs}
        return self.gold_titles - titles

    @property
    def all_gold_titles_present(self) -> bool:
        return not self.missing_gold_titles


@dataclasses.dataclass(frozen=True, slots=True)
class SpanTarget:
    """Shape of the answer-span -> chunk mapping.

    The left half is known at acquisition time (which gold sentence the answer
    depends on). The right half is retrieval-owned and stays ``None`` here:
    ``chunk_key``/``chunk_text`` are filled when retrieval runs, and nothing in
    this ticket resolves, ranks or scores them.
    """

    record_id: str
    gold_title: str
    sentence_id: int
    sentence_text: str
    chunk_key: str | None = None
    chunk_text: str | None = None

    @property
    def is_resolved(self) -> bool:
        """False until retrieval assigns a chunk. True here would mean a bug."""
        return self.chunk_key is not None


def span_map(record: HotpotRecord) -> tuple[SpanTarget, ...]:
    """Build the unfilled span map for one record (one target per gold sentence).

    A gold title missing from the candidate set yields an empty
    ``sentence_text`` rather than raising, so one ``fullwiki`` record with an
    unresolvable gold span cannot abort a whole-split load. The audit counts
    these separately as ``n_sentence_ids_out_of_range`` and
    ``n_records_missing_gold_title``.
    """
    paragraphs = {p.title: p for p in record.paragraphs}
    targets: list[SpanTarget] = []
    for fact in record.supporting_facts:
        paragraph = paragraphs.get(fact.title)
        sentences = paragraph.sentences if paragraph is not None else ()
        text = sentences[fact.sentence_id] if 0 <= fact.sentence_id < len(sentences) else ""
        targets.append(
            SpanTarget(
                record_id=record.record_id,
                gold_title=fact.title,
                sentence_id=fact.sentence_id,
                sentence_text=text,
            )
        )
    return tuple(targets)


def file_sha256(path: pathlib.Path) -> str:
    """Streaming sha256 of one file (never load a 160MB shard into memory)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_path(config: str, split: str, stem: str) -> pathlib.Path:
    """Resolve one parquet file, refusing an unknown config or split."""
    try:
        files = CONFIG_SPLIT_FILES[config][split]
    except KeyError:
        raise ValueError(
            f"unknown config/split {config!r}/{split!r}; acquired pairs are "
            f"{ {c: list(s) for c, s in CONFIG_SPLIT_FILES.items()} }"
        ) from None
    if stem not in files:
        raise ValueError(f"{stem!r} is not a file of {config!r}/{split!r}")
    return RAW_ROOT / config / f"{stem}.parquet"


def verify_split_hashes(config: str, split: str) -> dict[str, str]:
    """Verify every file of a split against :data:`FILE_SHA256`.

    Raises on the first drift, naming the file and both hashes, so a silent
    data swap can never quietly move eval numbers.
    """
    verified: dict[str, str] = {}
    for stem in CONFIG_SPLIT_FILES[config][split]:
        path = file_path(config, split, stem)
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing - dataset not fully acquired "
                f"(see {PROVENANCE_PATH})"
            )
        expected = FILE_SHA256[config][stem]
        actual = file_sha256(path)
        if actual != expected:
            raise RuntimeError(
                f"HotpotQA data drift in {path.name}: {actual} != {expected}"
            )
        verified[stem] = actual
    return verified


def _row_to_record(config: str, split: str, row: dict[str, object]) -> HotpotRecord:
    """Convert one decoded parquet row into a :class:`HotpotRecord`."""
    raw_context = row.get("context")
    context = raw_context if isinstance(raw_context, dict) else {}
    titles = context.get("title") or []
    all_sentences = context.get("sentences") or []
    paragraphs = tuple(
        Paragraph(
            title=str(title),
            sentences=tuple(str(s) for s in (sentences or [])),
        )
        for title, sentences in zip(titles, all_sentences, strict=False)
    )

    raw_facts = row.get("supporting_facts")
    facts = raw_facts if isinstance(raw_facts, dict) else {}
    fact_titles = facts.get("title") or []
    fact_sent_ids = facts.get("sent_id") or []
    supporting = tuple(
        SupportingFact(title=str(title), sentence_id=int(sent_id))
        for title, sent_id in zip(fact_titles, fact_sent_ids, strict=False)
    )

    answer = row.get("answer")
    question_type = row.get("type")
    level = row.get("level")
    return HotpotRecord(
        record_id=str(row.get("id", "")),
        config=config,
        split=split,
        question=str(row.get("question", "")),
        answer=None if answer is None else str(answer),
        question_type=None if question_type is None else str(question_type),
        level=None if level is None else str(level),
        supporting_facts=supporting,
        paragraphs=paragraphs,
        context_ragged=len(titles) != len(all_sentences),
    )


def iter_records(
    split: str,
    *,
    config: str = DEFAULT_CONFIG,
    verify_hashes: bool = True,
    limit: int | None = None,
) -> Iterator[HotpotRecord]:
    """Stream raw records for one split, hash-verified by default.

    Streaming keeps peak memory independent of split size: the 90k-row train
    split is never materialised unless the caller asks for it via
    :func:`load_split`.
    """
    if verify_hashes:
        verify_split_hashes(config, split)
    emitted = 0
    for stem in CONFIG_SPLIT_FILES[config][split]:
        parquet = pq.ParquetFile(str(file_path(config, split, stem)))
        for batch in parquet.iter_batches(batch_size=_BATCH_ROWS):
            for row in batch.to_pylist():
                yield _row_to_record(config, split, row)
                emitted += 1
                if limit is not None and emitted >= limit:
                    return


def load_split(
    split: str,
    *,
    config: str = DEFAULT_CONFIG,
    verify_hashes: bool = True,
    limit: int | None = None,
) -> list[HotpotRecord]:
    """Materialise one split. Prefer :func:`iter_records` for large splits."""
    return list(iter_records(split, config=config, verify_hashes=verify_hashes, limit=limit))


@dataclasses.dataclass(frozen=True, slots=True)
class SplitAudit:
    """Audit of one split. Every count here is an acquisition fact."""

    config: str
    split: str
    n_records: int
    n_labeled: int
    n_unlabeled: int
    #: Records whose candidate set is not the full 10 paragraphs. Counted over
    #: ALL records, labeled or not — a short candidate set is a property of the
    #: candidate set alone.
    n_records_not_10_paragraphs: int
    #: Records whose distractor count is not 8. Counted over LABELED records
    #: only: a distractor is "a candidate paragraph that is not gold", which is
    #: undefined for the unlabeled ``fullwiki`` test split (it has no gold, so
    #: every candidate would vacuously count as a distractor).
    n_records_not_8_distractors: int
    #: Records the distractor metric above was applied to, i.e. the labeled
    #: count. Reported so ``n_records_not_8_distractors`` cannot be misread as
    #: covering the whole split.
    n_distractor_metric_applicable: int
    #: Records with a supporting-fact title absent from the candidate set.
    n_records_missing_gold_title: int
    #: Individual (record, fact) pairs whose sentence_id falls outside its
    #: paragraph, i.e. whose gold span cannot be resolved.
    n_sentence_ids_out_of_range: int
    #: Records whose title list and sentence-list column disagreed in length.
    n_records_ragged_context: int
    n_duplicate_record_ids: int
    paragraph_count_histogram: dict[int, int]
    distractor_count_histogram: dict[int, int]
    question_type_counts: dict[str, int]
    level_counts: dict[str, int]

    @property
    def is_clean(self) -> bool:
        """True when every structural count that CAN be zero, is zero.

        Deliberately excludes ``n_records_not_10_paragraphs`` and
        ``n_records_not_8_distractors``: those are non-zero in the upstream
        data for every config, so folding them in would make ``is_clean``
        permanently false and hide the difference between a cosmetic shortfall
        (a short candidate set) and a real defect (an unresolvable gold span).
        """
        return (
            self.n_records_missing_gold_title == 0
            and self.n_sentence_ids_out_of_range == 0
            and self.n_records_ragged_context == 0
            and self.n_duplicate_record_ids == 0
        )


def audit_split(
    split: str,
    *,
    config: str = DEFAULT_CONFIG,
    verify_hashes: bool = True,
) -> SplitAudit:
    """Audit one split. Reads every record; mutates nothing and scores nothing."""
    if config not in CONFIG_SPLIT_FILES or split not in CONFIG_SPLIT_FILES[config]:
        raise ValueError(
            f"unknown config/split {config!r}/{split!r}; acquired pairs are "
            f"{ {c: list(s) for c, s in CONFIG_SPLIT_FILES.items()} }"
        )

    n_records = n_labeled = n_unlabeled = 0
    n_not_10 = n_not_8 = n_missing_gold = n_bad_sentence = 0
    n_ragged = n_duplicate_ids = 0
    paragraph_hist: dict[int, int] = {}
    distractor_hist: dict[int, int] = {}
    type_counts: dict[str, int] = {}
    level_counts: dict[str, int] = {}
    seen_ids: set[str] = set()

    for record in iter_records(split, config=config, verify_hashes=verify_hashes):
        n_records += 1
        n_labeled += int(record.is_labeled)
        n_unlabeled += int(not record.is_labeled)
        n_ragged += int(record.context_ragged)

        n_paragraphs = record.n_paragraphs
        paragraph_hist[n_paragraphs] = paragraph_hist.get(n_paragraphs, 0) + 1
        n_not_10 += int(n_paragraphs != EXPECTED_PARAGRAPHS_PER_RECORD)

        n_distractors = record.n_distractor_paragraphs
        if record.is_labeled:
            distractor_hist[n_distractors] = distractor_hist.get(n_distractors, 0) + 1
            n_not_8 += int(n_distractors != EXPECTED_DISTRACTOR_PARAGRAPHS_PER_RECORD)
            if record.missing_gold_titles:
                n_missing_gold += 1
            n_bad_sentence += sum(1 for t in span_map(record) if not t.sentence_text)

        key = record.question_type or "<none>"
        type_counts[key] = type_counts.get(key, 0) + 1
        level_key = record.level or "<none>"
        level_counts[level_key] = level_counts.get(level_key, 0) + 1

        if record.record_id in seen_ids:
            n_duplicate_ids += 1
        seen_ids.add(record.record_id)

    return SplitAudit(
        config=config,
        split=split,
        n_records=n_records,
        n_labeled=n_labeled,
        n_unlabeled=n_unlabeled,
        n_records_not_10_paragraphs=n_not_10,
        n_records_not_8_distractors=n_not_8,
        n_distractor_metric_applicable=n_labeled,
        n_records_missing_gold_title=n_missing_gold,
        n_sentence_ids_out_of_range=n_bad_sentence,
        n_records_ragged_context=n_ragged,
        n_duplicate_record_ids=n_duplicate_ids,
        paragraph_count_histogram=dict(sorted(paragraph_hist.items())),
        distractor_count_histogram=dict(sorted(distractor_hist.items())),
        question_type_counts=dict(sorted(type_counts.items())),
        level_counts=dict(sorted(level_counts.items())),
    )


def audit_all(
    *,
    config: str = DEFAULT_CONFIG,
    verify_hashes: bool = True,
) -> dict[str, SplitAudit]:
    """Audit every split of one config, in the fixed order of its manifest."""
    return {
        split: audit_split(split, config=config, verify_hashes=verify_hashes)
        for split in CONFIG_SPLIT_FILES[config]
    }


def audit_everything(*, verify_hashes: bool = True) -> dict[str, dict[str, SplitAudit]]:
    """Audit every acquired (config, split) pair."""
    return {
        config: audit_all(config=config, verify_hashes=verify_hashes)
        for config in CONFIG_SPLIT_FILES
    }


def labeled_splits(config: str = DEFAULT_CONFIG) -> tuple[str, ...]:
    """Splits carrying answers + supporting facts, i.e. the scorable ones.

    Derived by loading the first record of each split rather than hard-coded,
    so it cannot drift from the data.
    """
    labeled: list[str] = []
    for split in CONFIG_SPLIT_FILES[config]:
        for record in iter_records(
            split, config=config, verify_hashes=False, limit=1
        ):
            if record.is_labeled:
                labeled.append(split)
            break
    return tuple(labeled)


def audit_report_json(*, verify_hashes: bool = True) -> str:
    """Audit every acquired pair as JSON - the number record for the handoff."""
    everything = audit_everything(verify_hashes=verify_hashes)
    return json.dumps(
        {
            "dataset": DATASET_REPO,
            "revision": DATASET_REVISION,
            "license": DATASET_LICENSE,
            "default_config": DEFAULT_CONFIG,
            "expected_paragraphs_per_record": EXPECTED_PARAGRAPHS_PER_RECORD,
            "expected_distractor_paragraphs_per_record": (
                EXPECTED_DISTRACTOR_PARAGRAPHS_PER_RECORD
            ),
            "labeled_splits": {
                config: list(labeled_splits(config)) for config in CONFIG_SPLIT_FILES
            },
            "total_records": sum(
                audit.n_records
                for splits in everything.values()
                for audit in splits.values()
            ),
            "audits": {
                config: {
                    split: dataclasses.asdict(audit) for split, audit in splits.items()
                }
                for config, splits in everything.items()
            },
            "span_map_note": (
                "SpanTarget.chunk_key/chunk_text are None by design: retrieval "
                "fills them in a later ticket. Nothing here is scored."
            ),
        },
        indent=2,
        sort_keys=True,
    )


def _main() -> None:
    """CLI: print the audit report. Audit only - no scoring, no retrieval."""
    print(audit_report_json(verify_hashes=True))


if __name__ == "__main__":  # pragma: no cover - CLI entry
    _main()