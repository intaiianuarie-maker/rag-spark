"""2WikiMultihopQA raw record loader + evidence-path audit.

T-EVAL-002ACQUIRE. ACQUISITION AND RAW LOADING ONLY.

This module stops exactly where scoring begins. It converts the on-disk
parquet into typed records (question, answer, Wikidata evidence triples,
supporting facts, the per-title context) and audits whether each record's
evidence path resolves against the contexts it ships with. It deliberately
does NOT retrieve, embed, rank or score anything:

  * no bge-m3 / any embedder call — real bge-m3 runs when retrieval runs;
  * no metric function of any kind — the official EM/F1 metrics for this
    corpus belong to the T-EVAL-007 harness (``evals/leaderboard.py`` and
    ``evals/vendor/``), which resolves them through the vendor registry;
  * no ingestion — this dataset is scratch evaluation material, never live
    knowledge (see ``LICENSE-AND-PROVENANCE.md``).

The audit is the 2Wiki analogue of the HotpotQA fullwiki 5316 finding: it
counts, exactly, the supporting facts whose ``(title, sent_id)`` does not
resolve against the record's own ``context``. Those counts are reported by
:func:`audit_evidence_paths` and pinned in the tests — they are measured, never
asserted to be zero.

Memory: ``train`` is 167k records with full contexts (~330 MB of parquet), so
:func:`iter_records` streams row-group batches. Materializing the whole train
split with ``read_table().to_pylist()`` is killed by the OOM killer here (exit
137, measured); the streaming path peaks around 300 MB RSS.

Provenance: every file is pinned to dataset revision
``fe713bfbd1afbca1a65246741a75890405d56a3a`` and verified by sha256 on load
against :data:`FILE_SHA256`, so a drifted or swapped file fails loudly instead
of silently changing eval numbers (the pattern dev2 used in
``evals/hotpot_full.py``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib
from collections.abc import Iterator, Sequence

import pyarrow.parquet as pq

#: On-disk location of the acquired dataset.
WIKIMULTIHOP_ROOT = pathlib.Path("/root/code/Super Rag/datasets/2wikimultihopqa")
#: Directory holding the pinned parquet files.
RAW_ROOT = WIKIMULTIHOP_ROOT / "raw"
#: License + provenance record that must travel with the data.
PROVENANCE_PATH = WIKIMULTIHOP_ROOT / "LICENSE-AND-PROVENANCE.md"
#: Verbatim Apache-2.0 text copied from the upstream repository.
LICENSE_PATH = WIKIMULTIHOP_ROOT / "LICENSE-APACHE-2.0.txt"
#: The dataset card as served by the hub (license claim is in the prose).
CARD_PATH = WIKIMULTIHOP_ROOT / "HF-CARD-README.framolfese.md"
#: The hub's dataset metadata (no ``license:`` field — see the provenance doc).
HUB_METADATA_PATH = WIKIMULTIHOP_ROOT / "hf-dataset-metadata.json"

#: Hugging Face dataset coordinates the files were pulled from. The card
#: states this repo only repackages Alab-NII/2wikimultihop into the HotpotQA
#: field layout, with the content unaltered.
DATASET_REPO = "framolfese/2WikiMultihopQA"
DATASET_REVISION = "fe713bfbd1afbca1a65246741a75890405d56a3a"
#: Apache-2.0, resolved from the upstream repository (GitHub reports
#: ``spdx_id: Apache-2.0``) and the verbatim LICENSE file copied next to the
#: data. The hub itself carries no license metadata.
DATASET_LICENSE = "apache-2.0"
UPSTREAM_REPO = "https://github.com/Alab-NII/2wikimultihop"
PAPER = "https://aclanthology.org/2020.coling-main.580/"

#: Split -> parquet files, in a fixed order. The hub repackages the original
#: division: train 167454, validation 12576, test 12576.
SPLIT_FILES: dict[str, tuple[str, ...]] = {
    "train": ("train-00000-of-00002.parquet", "train-00001-of-00002.parquet"),
    "validation": ("validation-00000-of-00001.parquet",),
    # 2Wiki test is UNLABELED upstream: no answers, no supporting facts.
    "test": ("test-00000-of-00001.parquet",),
}

#: Split -> total rows across its files (pinned from parquet metadata).
SPLIT_ROWS: dict[str, int] = {
    "train": 167_454,
    "validation": 12_576,
    "test": 12_576,
}

#: sha256 of each file at :data:`DATASET_REVISION`. These are the LFS object
#: ids published by the hub, i.e. the upstream hashes rather than merely our
#: own record of whatever happened to arrive on disk.
FILE_SHA256: dict[str, tuple[str, ...]] = {
    "train": (
        "954842f2fade23d31438483304b298626adee4767bb467c2ae78cc5ce313d613",
        "7ca68091af5fa159351a36c1479f398b3e7b47d3c85f2d0d691406d301ba41c3",
    ),
    "validation": (
        "408e2dbb28edc6c8b9ca3ba0c94d4fc7bf17ffb923766593a3a7f546ab4cba59",
    ),
    "test": (
        "f4211d8bafa6291e13f8dcb3b1e82422d9da123d193050c135d1e72f28a9fcd1",
    ),
}

#: Fields every record must carry; a record missing any of them is a
#: provenance defect, counted by :func:`audit_evidence_paths`.
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "question",
    "answer",
    "type",
    "evidences",
    "supporting_facts",
    "context",
)

#: Default number of parquet rows pulled into memory per batch.
DEFAULT_BATCH_ROWS = 2_000


class CorpusPinError(RuntimeError):
    """Raised when an on-disk file does not match its pinned sha256."""


@dataclasses.dataclass(frozen=True)
class SupportingFact:
    """One evidence path: a context title plus a sentence index in it."""

    title: str
    sent_id: int


@dataclasses.dataclass(frozen=True)
class Resolution:
    """Why (or whether) one evidence path resolves against its own contexts."""

    title: str
    sent_id: int
    resolved: bool
    #: "" when resolved, else "title_not_in_context" / "sent_id_out_of_range".
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class WikiMultiHopRecord:
    """One 2WikiMultihopQA instance, verbatim — no retrieval, no scoring."""

    id: str
    question: str
    answer: str
    type: str
    evidences: tuple[tuple[str, ...], ...]
    supporting_facts: tuple[SupportingFact, ...]
    #: Context title -> its sentences. When a title is listed twice the first
    #: copy is kept (the copies are measured to be identical upstream — see
    #: :func:`audit_evidence_paths`); the repeats are counted in
    #: :attr:`duplicate_titles`.
    context: dict[str, tuple[str, ...]]
    split: str
    #: Titles the context lists more than once.
    duplicate_titles: tuple[str, ...] = ()
    #: Duplicates whose sentence lists DISAGREE — the only case that makes a
    #: ``sent_id`` ambiguous.
    conflicting_duplicate_titles: tuple[str, ...] = ()
    #: Required fields absent or null in the raw parquet row. An empty answer
    #: or empty supporting_facts is not "missing" (the unlabeled test split is
    #: legitimately unlabeled).
    missing_fields: tuple[str, ...] = ()

    def resolve(self, fact: SupportingFact) -> Resolution:
        """Resolve one evidence path against this record's contexts."""
        sentences = self.context.get(fact.title)
        if sentences is None:
            return Resolution(fact.title, fact.sent_id, False, "title_not_in_context")
        if not 0 <= fact.sent_id < len(sentences):
            return Resolution(fact.title, fact.sent_id, False, "sent_id_out_of_range")
        return Resolution(fact.title, fact.sent_id, True)

    def unresolved(self) -> tuple[Resolution, ...]:
        """Evidence paths that do not resolve (expected: none on 2Wiki)."""
        return tuple(r for r in (self.resolve(f) for f in self.supporting_facts) if not r.resolved)

    def resolved_paths(self) -> tuple[SupportingFact, ...]:
        """The evidence paths that resolve against this record's contexts."""
        return tuple(f for f in self.supporting_facts if self.resolve(f).resolved)

    def supporting_text(self) -> tuple[str, ...]:
        """The supporting sentences themselves, in supporting-fact order."""
        out: list[str] = []
        for fact in self.supporting_facts:
            resolution = self.resolve(fact)
            if resolution.resolved:
                out.append(self.context[fact.title][fact.sent_id])
        return tuple(out)


@dataclasses.dataclass(frozen=True)
class EvidenceAudit:
    """Exact counts from one evidence-path audit pass over a split."""

    split: str
    n_records: int
    n_records_with_supporting_facts: int
    n_supporting_facts: int
    n_resolvable: int
    #: Evidence paths that do not resolve, split by reason.
    n_unresolvable: int
    n_title_not_in_context: int
    n_sent_id_out_of_range: int
    #: Records with >=1 unresolvable path.
    n_records_unresolvable: int
    #: Records whose context lists the same title more than once, and of those
    #: how many duplicates disagree about the sentence list (ambiguity).
    n_records_duplicate_title: int
    n_duplicate_title_groups: int
    n_duplicate_title_groups_conflicting: int
    #: Records missing a required field (provenance defect).
    n_records_missing_field: int
    #: Records with no answer / no supporting facts (the unlabeled test split).
    n_records_without_answer: int

    @property
    def unresolvable(self) -> int:
        """The headline audit number, measured rather than assumed."""
        return self.n_unresolvable


def sha256_of(path: pathlib.Path, chunk: int = 1 << 20) -> str:
    """sha256 of a file, streamed."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_pins(split: str | None = None) -> dict[str, str]:
    """Verify every pinned file against :data:`FILE_SHA256`.

    Returns the ``filename -> sha256`` map verified. Raises
    :class:`CorpusPinError` on the first mismatch, so a drifted or swapped
    corpus fails loudly rather than quietly changing eval numbers.
    """
    splits = [split] if split else list(SPLIT_FILES)
    verified: dict[str, str] = {}
    for name in splits:
        files = SPLIT_FILES[name]
        expected = FILE_SHA256[name]
        if len(files) != len(expected):  # pragma: no cover - registry invariant
            raise CorpusPinError(f"{name}: {len(files)} files but {len(expected)} pins")
        for filename, want in zip(files, expected, strict=True):
            path = RAW_ROOT / filename
            if not path.exists():
                raise CorpusPinError(f"missing pinned file: {path}")
            got = sha256_of(path)
            if got != want:
                raise CorpusPinError(f"{path}: sha256 {got} != pinned {want}")
            verified[filename] = got
    return verified


def row_counts(split: str) -> int:
    """Rows in a split, read from parquet metadata (no data read)."""
    total = 0
    for filename in SPLIT_FILES[split]:
        total += pq.ParquetFile(RAW_ROOT / filename).metadata.num_rows
    return total


def split_of(filename: str) -> str:
    """The split a pinned file belongs to."""
    for split, files in SPLIT_FILES.items():
        if filename in files:
            return split
    raise KeyError(f"{filename!r} is not a pinned file")


def _to_record(row: dict, split: str) -> WikiMultiHopRecord:
    supporting = row["supporting_facts"]
    facts = tuple(
        SupportingFact(title=title, sent_id=int(sent_id))
        for title, sent_id in zip(
            supporting["title"], supporting["sent_id"], strict=False
        )
    )
    ctx = row["context"]
    pairs: list[tuple[str, tuple[str, ...]]] = [
        (title, tuple(sentences))
        for title, sentences in zip(ctx["title"], ctx["sentences"], strict=False)
    ]
    repeats: dict[str, list[tuple[str, ...]]] = {}
    context: dict[str, tuple[str, ...]] = {}
    for title, sentences in pairs:
        repeats.setdefault(title, []).append(sentences)
        context.setdefault(title, sentences)
    return WikiMultiHopRecord(
        id=row["id"] or "",
        question=row["question"] or "",
        answer=row["answer"] or "",
        type=row["type"] or "",
        evidences=tuple(tuple(triple) for triple in row["evidences"]),
        supporting_facts=facts,
        context=context,
        split=split,
        duplicate_titles=tuple(
            title for title, copies in repeats.items() if len(copies) > 1
        ),
        conflicting_duplicate_titles=tuple(
            title
            for title, copies in repeats.items()
            if len(copies) > 1 and not all(copy == copies[0] for copy in copies)
        ),
        missing_fields=tuple(field for field in REQUIRED_FIELDS if row.get(field) is None),
    )


def iter_records(
    split: str,
    limit: int | None = None,
    batch_rows: int = DEFAULT_BATCH_ROWS,
) -> Iterator[WikiMultiHopRecord]:
    """Stream records of a split, file by file and batch by batch.

    Streaming is required, not stylistic: materializing ``train`` in one go is
    OOM-killed on this host (measured, exit 137).
    """
    if split not in SPLIT_FILES:
        raise KeyError(f"unknown split {split!r}; known: {sorted(SPLIT_FILES)}")
    emitted = 0
    for filename in SPLIT_FILES[split]:
        parquet = pq.ParquetFile(RAW_ROOT / filename)
        columns = ["id", "question", "answer", "type", "evidences", "supporting_facts", "context"]
        for batch in parquet.iter_batches(batch_size=batch_rows, columns=columns):
            for row in batch.to_pylist():
                yield _to_record(row, split)
                emitted += 1
                if limit is not None and emitted >= limit:
                    return


def load_records(
    split: str = "validation",
    limit: int | None = None,
    verify: bool = True,
) -> list[WikiMultiHopRecord]:
    """Materialize a split (small ones), verifying the pins first."""
    if verify:
        verify_pins(split)
    return list(iter_records(split, limit=limit))


def audit_evidence_paths(
    split: str = "validation",
    limit: int | None = None,
    verify: bool = True,
) -> EvidenceAudit:
    """Audit every record's evidence path against its own contexts.

    Counts are measured, not assumed. On the labeled 2Wiki splits every
    supporting fact resolves (the candidate set ships with the gold spans, as
    HotpotQA's ``distractor`` config does — unlike ``fullwiki``, where 5316 of
    7405 validation records had a gold title missing from the candidate set).
    Duplicate context titles do occur; this pass also measures whether they
    disagree about the sentence list, which would make a ``sent_id``
    ambiguous.
    """
    if verify:
        verify_pins(split)
    n_records = n_with_sf = n_sf = n_res = n_unres = 0
    n_title_missing = n_sid_oob = n_rec_unres = 0
    n_dup_rec = n_dup_groups = n_dup_conflicting = 0
    n_missing_field = n_without_answer = 0
    for record in iter_records(split, limit=limit):
        n_records += 1
        if record.missing_fields:
            n_missing_field += 1
        if not record.answer:
            n_without_answer += 1
        n_dup_rec += 1 if record.duplicate_titles else 0
        n_dup_groups += len(record.duplicate_titles)
        n_dup_conflicting += len(record.conflicting_duplicate_titles)
        if not record.supporting_facts:
            continue
        n_with_sf += 1
        n_sf += len(record.supporting_facts)
        record_bad = False
        for resolution in (record.resolve(f) for f in record.supporting_facts):
            if resolution.resolved:
                n_res += 1
                continue
            record_bad = True
            n_unres += 1
            if resolution.reason == "title_not_in_context":
                n_title_missing += 1
            else:
                n_sid_oob += 1
        if record_bad:
            n_rec_unres += 1
    return EvidenceAudit(
        split=split,
        n_records=n_records,
        n_records_with_supporting_facts=n_with_sf,
        n_supporting_facts=n_sf,
        n_resolvable=n_res,
        n_unresolvable=n_unres,
        n_title_not_in_context=n_title_missing,
        n_sent_id_out_of_range=n_sid_oob,
        n_records_unresolvable=n_rec_unres,
        n_records_duplicate_title=n_dup_rec,
        n_duplicate_title_groups=n_dup_groups,
        n_duplicate_title_groups_conflicting=n_dup_conflicting,
        n_records_missing_field=n_missing_field,
        n_records_without_answer=n_without_answer,
    )


def audit_duplicate_context_titles(
    split: str = "validation",
    limit: int | None = None,
) -> tuple[int, int]:
    """``(groups, conflicting_groups)`` for repeated context titles.

    A repeated title is only an audit problem when the copies disagree about
    their sentence list, because then ``sent_id`` points at different text
    depending on which copy a retriever picked up. Measured on validation:
    523 repeats, 0 conflicting (the copies are byte-identical).
    """
    groups = conflicting = 0
    for record in iter_records(split, limit=limit):
        groups += len(record.duplicate_titles)
        conflicting += len(record.conflicting_duplicate_titles)
    return groups, conflicting


__all__: Sequence[str] = (
    "CARD_PATH",
    "DATASET_LICENSE",
    "DATASET_REPO",
    "DATASET_REVISION",
    "DEFAULT_BATCH_ROWS",
    "FILE_SHA256",
    "HUB_METADATA_PATH",
    "LICENSE_PATH",
    "PAPER",
    "PROVENANCE_PATH",
    "RAW_ROOT",
    "REQUIRED_FIELDS",
    "SPLIT_FILES",
    "SPLIT_ROWS",
    "UPSTREAM_REPO",
    "WIKIMULTIHOP_ROOT",
    "CorpusPinError",
    "EvidenceAudit",
    "Resolution",
    "SupportingFact",
    "WikiMultiHopRecord",
    "audit_duplicate_context_titles",
    "audit_evidence_paths",
    "iter_records",
    "load_records",
    "row_counts",
    "sha256_of",
    "split_of",
    "verify_pins",
)