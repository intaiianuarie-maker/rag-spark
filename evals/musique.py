"""MuSiQue raw record loader + evidence-path audit.

T-EVAL-003ACQUIRE. ACQUISITION AND RAW LOADING ONLY.

This module stops exactly where scoring begins. It converts the on-disk JSONL
(acquired at pinned HF revision) into typed records — the per-paragraph
candidate context with the ``is_supporting`` flag, the multi-hop
``question_decomposition`` whose ``paragraph_support_idx`` points at the
paragraph that supports each hop, plus question / answer / aliases — and audits
whether each hop's evidence path resolves against the paragraph set the record
ships with. It deliberately does NOT retrieve, embed, rank or score anything:

  * no bge-m3 / any embedder call — real bge-m3 runs when retrieval runs;
  * no metric function of any kind — the official EM/F1 metrics for this
    corpus belong to the T-EVAL-007 harness, which resolves them through the
    vendor registry;
  * no ingestion — this dataset is scratch evaluation material, never live
    knowledge (see ``LICENSE-AND-PROVENANCE.md``).

Two configs are acquired, and the difference is NOT cosmetic:

  ``ans``   the 2 417-record answerable subset — every record is answerable,
            every hop carries a supporting-paragraph index, and each hop
            resolves cleanly against the record's own paragraphs.

  ``full``  the 4 834-record "hard" set. Each question id appears twice: once
            as the answerable variant (full hop chain, ``is_supporting`` spans
            present, ``paragraph_support_idx`` filled) and once as the
            unanswerable variant (same question text but answerability
            removed — hops carry ``paragraph_support_idx = null`` and no
            supporting paragraphs). 2 417 answerable + 2 417 unanswerable.

Audit: a decomposition hop's ``paragraph_support_idx`` indexes into the record's
``paragraphs`` list. The audit resolves every such index, counts resolvable vs
unresolvable evidence paths (hops with a null index — the unanswerable case —
are counted separately and excluded from that split), and tallies the record /
answerable / hop / supporting-paragraph numbers pinned in the tests. The
headline finding, measured across both configs, is **0 unresolvable evidence
paths** — a counter that is proven live (not zero by fiat) by a synthetic
record test.

Provenance: every file is pinned to dataset revision
``c8f4f8c9465fb69d31a8eae894c3fd509c4ca321`` and verified by sha256 on load
against :data:`FILE_SHA256`, so a drifted or swapped file fails loudly instead
of silently changing eval numbers (the pattern already used by
``evals/wikimultihop.py`` and ``evals/hotpot_full.py``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
from collections import Counter
from collections.abc import Iterator
from typing import Any

#: On-disk location of the acquired dataset.
MUSIQUE_ROOT = pathlib.Path("/root/code/Super Rag/datasets/musique")
#: Directory holding the pinned JSONL files, one per config.
RAW_ROOT = MUSIQUE_ROOT / "raw"
#: License + provenance record that must travel with the data.
PROVENANCE_PATH = MUSIQUE_ROOT / "LICENSE-AND-PROVENANCE.md"

#: Hugging Face dataset coordinates the files were pulled from.
DATASET_REPO = "dgslibisey/MuSiQue"
DATASET_REVISION = "c8f4f8c9465fb69d31a8eae894c3fd509c4ca321"
#: CC-BY-4.0 upstream (TACL 2022).
DATASET_LICENSE = "cc-by-4.0"
UPSTREAM_REPO = "https://github.com/stonybrooknlp/musique"
PAPER = "https://aclananthology.org/2022.tacl-1.27/"

#: Config loaded when a caller does not name one. ``ans`` is the clean,
#: fully-answerable scorable subset (the analogue of HotpotQA's ``distractor``
#: candidate set, where every gold span is present).
DEFAULT_CONFIG = "ans"

#: sha256 of each file at :data:`DATASET_REVISION`. These are the LFS object ids
#: published by the hub — the upstream hashes rather than merely a record of
#: whatever happened to arrive on disk.
FILE_SHA256: dict[str, dict[str, str]] = {
    "ans": {
        "musique_ans_v1.0_dev.jsonl":
            "15fa63794d18a94ce12411aca6e2327e65b6e83b0b1490efab3f1962e48abf3b",
    },
    "full": {
        "musique_full_v1.0_dev.jsonl":
            "8cab31d56a3a1c4ef491b205a8dab3f1ac9c66e472098c6cf1de4e20294f7a4a",
    },
}

#: Config -> pinned files, in the fixed (only) order each config is read in.
CONFIG_FILES: dict[str, tuple[str, ...]] = {
    "ans": ("musique_ans_v1.0_dev.jsonl",),
    "full": ("musique_full_v1.0_dev.jsonl",),
}

#: Fields every record must carry; one absent/null is a provenance defect,
#: counted by :func:`audit_evidence_paths` as ``n_records_missing_field``.
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "paragraphs",
    "question",
    "question_decomposition",
    "answer",
    "answer_aliases",
    "answerable",
)


class CorpusPinError(RuntimeError):
    """Raised when an on-disk file is missing or does not match its pin.

    A single, explicit error type so a caller can tell a provenance defect
    (missing or drifted corpus) from any other failure and never silently
    fall back to unverified data.
    """


@dataclasses.dataclass(frozen=True)
class MuSiQueParagraph:
    """One candidate paragraph shipped with a MuSiQue record."""

    idx: int
    title: str
    paragraph_text: str
    is_supporting: bool

    @property
    def text(self) -> str:
        return self.paragraph_text


@dataclasses.dataclass(frozen=True)
class MuSiQueDecompositionHop:
    """One reasoning step from ``question_decomposition``.

    ``paragraph_support_idx`` is the index into the owning record's
    ``paragraphs`` list of the paragraph supporting this hop, or None when the
    hop has no supporting paragraph — which is the unanswerable case.
    """

    id: int
    question: str
    answer: str | None
    paragraph_support_idx: int | None


@dataclasses.dataclass(frozen=True)
class MuSiQueEvidenceResolution:
    """Why (or whether) one hop's ``paragraph_support_idx`` resolves."""

    hop: MuSiQueDecompositionHop
    resolved: bool
    #: "" when resolved, else "null_support_idx" / "idx_out_of_range" /
    #: "not_supporting".
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class MuSiQueRecord:
    """One raw MuSiQue instance, verbatim — no retrieval, no scoring."""

    id: str
    paragraphs: tuple[MuSiQueParagraph, ...]
    question: str
    question_decomposition: tuple[MuSiQueDecompositionHop, ...]
    answer: str
    answer_aliases: tuple[str, ...]
    answerable: bool
    config: str
    #: Required fields absent or null in the raw row (a provenance defect).
    missing_fields: tuple[str, ...] = ()

    @property
    def n_paragraphs(self) -> int:
        return len(self.paragraphs)

    @property
    def n_supporting_paragraphs(self) -> int:
        return sum(1 for p in self.paragraphs if p.is_supporting)

    @property
    def n_hops(self) -> int:
        return len(self.question_decomposition)

    def resolve(self, hop: MuSiQueDecompositionHop) -> MuSiQueEvidenceResolution:
        """Resolve one hop's evidence path against this record's paragraphs."""
        if hop.paragraph_support_idx is None:
            return MuSiQueEvidenceResolution(hop, False, "null_support_idx")
        idx = hop.paragraph_support_idx
        if idx < 0 or idx >= len(self.paragraphs):
            return MuSiQueEvidenceResolution(hop, False, "idx_out_of_range")
        if self.paragraphs[idx].is_supporting:
            return MuSiQueEvidenceResolution(hop, True)
        return MuSiQueEvidenceResolution(hop, False, "not_supporting")

    def unresolved(self) -> tuple[MuSiQueEvidenceResolution, ...]:
        """Hops that carry a supporting-paragraph index but fail to resolve.

        Mirrors 2Wiki's evidence-path audit: only hops that *declare* an
        evidence path (``paragraph_support_idx`` is not None) are eligible; a
        null index is the unanswerable case, not a broken path.
        """
        unresolved: list[MuSiQueEvidenceResolution] = []
        for hop in self.question_decomposition:
            if hop.paragraph_support_idx is None:
                continue
            resolution = self.resolve(hop)
            if not resolution.resolved:
                unresolved.append(resolution)
        return tuple(unresolved)

    @property
    def supporting_evidence(self) -> tuple[str, ...]:
        """The supporting paragraph texts, in hop order (resolved paths only)."""
        out: list[str] = []
        for hop in self.question_decomposition:
            resolution = self.resolve(hop)
            if resolution.resolved and hop.paragraph_support_idx is not None:
                out.append(self.paragraphs[hop.paragraph_support_idx].paragraph_text)
        return tuple(out)


@dataclasses.dataclass(frozen=True)
class MuSiQueAudit:
    """Exact counts from one evidence-path audit pass over a config."""

    config: str
    n_records: int
    n_answerable: int
    n_unanswerable: int
    n_hops: int
    n_records_with_decomposition: int
    #: number of decomposition hops per record -> count of records
    hop_count_histogram: dict[int, int]
    #: number of supporting paragraphs per record -> count of records
    supporting_paragraph_count_histogram: dict[int, int]
    #: number of candidate paragraphs per record -> count of records
    paragraph_count_histogram: dict[int, int]
    #: hops carrying a ``paragraph_support_idx`` (the evidence paths under test)
    n_evidence_paths: int
    #: hops with ``paragraph_support_idx == None`` (the unanswerable case)
    n_hops_null_idx: int
    n_resolvable: int
    #: evidence paths that do not resolve, split by failure mode
    n_unresolvable: int
    n_idx_out_of_range: int
    n_records_missing_field: int
    n_records_missing_decomposition: int

    @property
    def unresolvable(self) -> int:
        """Headline audit number, measured rather than assumed."""
        return self.n_unresolvable


def sha256_of(path: pathlib.Path, chunk: int = 1 << 20) -> str:
    """sha256 of a file, streamed so large files stay flat in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_path(config: str, filename: str) -> pathlib.Path:
    """The on-disk path for one pinned file, validating the config."""
    if config not in CONFIG_FILES:
        raise CorpusPinError(
            f"unknown config {config!r}; known: {sorted(CONFIG_FILES)}"
        )
    return RAW_ROOT / filename


def verify_pins(config: str = DEFAULT_CONFIG) -> dict[str, str]:
    """Verify every pinned file of a config against :data:`FILE_SHA256`.

    Returns the ``filename -> sha256`` map verified. Raises
    :class:`CorpusPinError` on the first missing or drifted file, so a silent
    corpus swap can never quietly move eval numbers.
    """
    if config not in FILE_SHA256:
        raise CorpusPinError(
            f"unknown config {config!r}; known: {sorted(FILE_SHA256)}"
        )
    verified: dict[str, str] = {}
    for filename, want in FILE_SHA256[config].items():
        path = RAW_ROOT / filename
        if not path.exists():
            raise CorpusPinError(f"missing pinned file: {path}")
        got = sha256_of(path)
        if got != want:
            raise CorpusPinError(f"{path}: sha256 {got} != pinned {want}")
        verified[filename] = got
    return verified


def count_records(config: str = DEFAULT_CONFIG, *, verify: bool = True) -> int:
    """Exact number of records in a config, streamed (never materialised)."""
    return sum(1 for _ in iter_records(config, verify=verify))


def _to_record(config: str, row: dict[str, Any]) -> MuSiQueRecord:
    """Convert one decoded JSONL row into a :class:`MuSiQueRecord`."""
    raw_paragraphs = row.get("paragraphs") or []
    paragraphs = tuple(
        MuSiQueParagraph(
            idx=int(p["idx"]),
            title=str(p["title"]),
            paragraph_text=str(p["paragraph_text"]),
            is_supporting=bool(p["is_supporting"]),
        )
        for p in raw_paragraphs
    )
    raw_decomp = row.get("question_decomposition") or []
    question_decomposition = tuple(
        MuSiQueDecompositionHop(
            id=int(h["id"]),
            question=str(h["question"]),
            answer=None if h.get("answer") is None else str(h["answer"]),
            paragraph_support_idx=(
                None
                if h.get("paragraph_support_idx") is None
                else int(h["paragraph_support_idx"])
            ),
        )
        for h in raw_decomp
    )
    answer = row.get("answer")
    answer_aliases = row.get("answer_aliases") or []
    return MuSiQueRecord(
        id=str(row.get("id", "") or ""),
        paragraphs=paragraphs,
        question=str(row.get("question", "") or ""),
        question_decomposition=question_decomposition,
        answer="" if answer is None else str(answer),
        answer_aliases=tuple(str(a) for a in answer_aliases),
        answerable=bool(row.get("answerable", False)),
        config=config,
        missing_fields=tuple(
            field for field in REQUIRED_FIELDS if row.get(field) is None
        ),
    )


def iter_records(
    config: str = DEFAULT_CONFIG,
    *,
    verify: bool = True,
    limit: int | None = None,
) -> Iterator[MuSiQueRecord]:
    """Stream raw records for a config, hash-verified by default.

    Streaming keeps memory flat regardless of file size — the 59 MB ``full``
    split is never fully materialised unless a caller collects it via
    :func:`load_records`.
    """
    if config not in FILE_SHA256:
        raise KeyError(f"unknown config {config!r}; known: {sorted(FILE_SHA256)}")
    if verify:
        verify_pins(config)
    emitted = 0
    for filename in FILE_SHA256[config]:
        path = RAW_ROOT / filename
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                yield _to_record(config, row)
                emitted += 1
                if limit is not None and emitted >= limit:
                    return


def load_records(
    config: str = DEFAULT_CONFIG,
    *,
    verify: bool = True,
    limit: int | None = None,
) -> list[MuSiQueRecord]:
    """Materialise a config (small ones), verifying the pins first."""
    if verify:
        verify_pins(config)
    return list(iter_records(config, verify=False, limit=limit))


def audit_evidence_paths(
    config: str = DEFAULT_CONFIG,
    *,
    verify: bool = True,
    limit: int | None = None,
) -> MuSiQueAudit:
    """Audit every record's evidence path against its own paragraphs.

    Counts are measured, not assumed. Each decomposition hop carries a
    ``paragraph_support_idx``; the audit resolves it against the record's
    paragraph list and counts resolvable vs unresolvable paths (hops whose
    index is null — the unanswerable case — are counted separately and
    excluded from the resolvable/unresolvable split), plus the record /
    answerable / hop / supporting-paragraph tallies pinned in the tests.
    """
    if config not in FILE_SHA256:
        raise KeyError(f"unknown config {config!r}; known: {sorted(FILE_SHA256)}")
    if verify:
        verify_pins(config)
    n_records = n_answerable = n_unanswerable = 0
    n_hops = n_with_decomp = 0
    n_missing_field = n_missing_decomp = 0
    n_evidence_paths = n_null_idx = 0
    n_resolvable = n_unresolvable = n_oob = 0
    hop_hist: Counter[int] = Counter()
    supp_hist: Counter[int] = Counter()
    para_hist: Counter[int] = Counter()
    for record in iter_records(config, verify=False, limit=limit):
        n_records += 1
        if record.answerable:
            n_answerable += 1
        else:
            n_unanswerable += 1
        n_missing_field += 1 if record.missing_fields else 0
        para_hist[len(record.paragraphs)] += 1
        supp_hist[record.n_supporting_paragraphs] += 1
        if record.question_decomposition:
            n_with_decomp += 1
        else:
            n_missing_decomp += 1
        hop_hist[len(record.question_decomposition)] += 1
        n_hops += len(record.question_decomposition)
        for hop in record.question_decomposition:
            if hop.paragraph_support_idx is None:
                n_null_idx += 1
                continue
            n_evidence_paths += 1
            resolution = record.resolve(hop)
            if resolution.resolved:
                n_resolvable += 1
            else:
                n_unresolvable += 1
                if resolution.reason == "idx_out_of_range":
                    n_oob += 1
    return MuSiQueAudit(
        config=config,
        n_records=n_records,
        n_answerable=n_answerable,
        n_unanswerable=n_unanswerable,
        n_hops=n_hops,
        n_records_with_decomposition=n_with_decomp,
        hop_count_histogram=dict(sorted(hop_hist.items())),
        supporting_paragraph_count_histogram=dict(sorted(supp_hist.items())),
        paragraph_count_histogram=dict(sorted(para_hist.items())),
        n_evidence_paths=n_evidence_paths,
        n_hops_null_idx=n_null_idx,
        n_resolvable=n_resolvable,
        n_unresolvable=n_unresolvable,
        n_idx_out_of_range=n_oob,
        n_records_missing_field=n_missing_field,
        n_records_missing_decomposition=n_missing_decomp,
    )


def audit_report_json(*, verify: bool = True) -> str:
    """Audit every config as JSON — the number record for the handoff."""
    return json.dumps(
        {
            "dataset": DATASET_REPO,
            "revision": DATASET_REVISION,
            "license": DATASET_LICENSE,
            "default_config": DEFAULT_CONFIG,
            "audits": {
                config: dataclasses.asdict(
                    audit_evidence_paths(config, verify=verify)
                )
                for config in FILE_SHA256
            },
            "note": (
                "MuSiQueAudit.unresolvable is a property, not a field, so it is "
                "omitted by asdict; n_unresolvable is the same value."
            ),
        },
        indent=2,
        sort_keys=True,
    )


def _main() -> None:
    """CLI: print the audit report. Audit only - no scoring, no retrieval."""
    print(audit_report_json(verify=True))


if __name__ == "__main__":  # pragma: no cover - CLI entry
    _main()


__all__: tuple[str, ...] = (
    "DATASET_LICENSE",
    "DATASET_REPO",
    "DATASET_REVISION",
    "DEFAULT_CONFIG",
    "FILE_SHA256",
    "MUSIQUE_ROOT",
    "PAPER",
    "PROVENANCE_PATH",
    "RAW_ROOT",
    "REQUIRED_FIELDS",
    "UPSTREAM_REPO",
    "CorpusPinError",
    "MuSiQueAudit",
    "MuSiQueDecompositionHop",
    "MuSiQueEvidenceResolution",
    "MuSiQueParagraph",
    "MuSiQueRecord",
    "audit_evidence_paths",
    "audit_report_json",
    "count_records",
    "iter_records",
    "load_records",
    "sha256_of",
    "verify_pins",
)
