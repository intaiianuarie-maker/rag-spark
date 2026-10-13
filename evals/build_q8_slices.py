"""Build Q8 pinned evaluation slices across EN, RO, PT, tables, and multi-source.

Pins verified extractive evaluation test slices where answers are supported
by context:
- EN: xquad-en-ro-v1 (xquad.en)
- RO: xquad-en-ro-v1 (xquad.ro)
- PT: pira-v1 (Portuguese QA with context-substantiated answers)
- tables: t2-finance-tests-v1 (TAT-DQA extractive table QA)
- multi: ragbench-tests-v1 (hotpotqa multi-source)
- unanswerable: authored in T-V1-075 (no public dataset ships questions that
  are unanswerable *from a specific context*); every record's ``gold_answer``
  is "" because no supportable answer exists

Output: evals/q8_eval_slices.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "packages", _REPO / "apps" / "api", _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.eval.metrics import score_faithfulness  # noqa: E402

_DATASETS_ROOT = Path("/root/code/Super Rag/datasets/rag-eval")
DEFAULT_OUT = Path(__file__).resolve().parent / "q8_eval_slices.jsonl"
DEFAULT_N_PER_SLICE = 10

SLICES = ("en", "ro", "pt", "tables", "multi", "unanswerable")
SEEDS = {
    "en": 20261012,
    "ro": 20261013,
    "pt": 20261014,
    "tables": 20261015,
    "multi": 20261016,
    "unanswerable": 20261017,
}


def _sha(q: str, c: str) -> str:
    return hashlib.sha256(f"{q}\n{c}".encode()).hexdigest()[:16]


#: T-V1-075 — the ``unanswerable`` slice. Authored here (rather than extracted)
#: because no public dataset ships questions that are unanswerable *from a
#: specific context*, which is what this slice must measure. Each row asks a
#: plausible question about a corpus-style context that simply does not contain
#: the answer, so ``gold_answer`` is empty by construction. Contexts are
#: deliberately drawn from the same domains as the extracted slices (finance
#: tables, climate, city description, film synopsis) in EN/RO/PT so abstention
#: is exercised across the same language spread.
UNANSWERABLE_ROWS: tuple[tuple[str, str, str], ...] = (
    (
        'unans-001',
        'What was the total revenue reported for fiscal 2021 in the segment table?',
        'Restructuring liabilities consist of (in thousands): current '
        'portion $1,204, non-current portion $963, total $2,167 as of the '
        'balance sheet date. The company completed a review of its lease '
        'portfolio during the period and recorded related impairment '
        'charges in cost of revenue.'
    ),
    (
        'unans-002',
        'Which specific county recorded the highest rainfall in the state during the study?',
        'The objective of this work is to describe the climate of the '
        'Espirito Santo region and its interannual variability. '
        'Precipitation is concentrated in the  austral summer, with a '
        'pronounced dry season between June and September. Station '
        'records were obtained from the national meteorological '
        'institute.'
    ),
    (
        'unans-003',
        'Cine a castigat primul Oscar in 1929 pentru regie?',
        'Jacksonville se află în regiunea First Coast din nord-estul '
        'Floridei, situată pe ambele maluri ale fluviului Saint Johns. '
        'Zona a fost inițial locuită de poporul Timucua, iar dezvoltarea '
        'sa a fost determinată de comerțul cu lemnul și de construcția '
        'căilor ferate.'
    ),
    (
        'unans-004',
        'What is the population of the city where this bridge is located?',
        'About Time is a 2013 British romantic comedy-drama film written '
        'and directed by Richard Curtis. The film stars Domhnall Gleeson '
        'and Ruth Wilson and was released by Universal Pictures to '
        'generally positive reviews from critics.'
    ),
    (
        'unans-005',
        'How many employees did the company have at the end of the reporting period?',
        'The accompanying notes describe the basis of preparation, '
        'including the adoption of recently issued accounting standards. '
        'All amounts are presented in thousands of US dollars unless '
        'otherwise indicated. The fiscal year ends on the thirtieth of '
        'June.'
    ),
    (
        'unans-006',
        'Qual foi o precipitao mais intensa registrada na estacao de VITORIA?',
        'O objetivo deste trabalho e descrever o clima da regiao e a sua '
        'variabilidade interanual. A precipitacao concentra-se no verao '
        'austral, com uma estacao seca pronunciada entre junho e '
        'setembro. Os registros foram obtidos junto ao instituto nacional '
        'de meteorologia.'
    ),
    (
        'unans-007',
        'Who served as chief executive during the period covered by these statements?',
        'Restructuring liabilities consist of (in thousands): current '
        'portion $1,204, non-current portion $963, total $2,167 as of the '
        'balance sheet date. Operating segments are reported on a '
        'consistent basis and reflect the manner in which the chief '
        'operating decision maker reviews performance.'
    ),
    (
        'unans-008',
        'Which award did the director of the film receive for this work?',
        'About Time is a 2013 British romantic comedy-drama film written '
        'and directed by Richard Curtis. The film stars Domhnall Gleeson '
        'and Ruth Wilson and was released by Universal Pictures to '
        'generally positive reviews from critics.'
    ),
    (
        'unans-009',
        'Ce stat a fost infiintat de regina Elisabeta a II-a in acest oras?',
        'Jacksonville se afla in regiunea First Coast din nord-estul '
        'Floridei, situat pe ambele maluri ale fluviului Saint Johns. '
        'Comunitatile din Jacksonville Beaches se aliniaza de-a lungul '
        'coastei atlantice alaturate.'
    ),
    (
        'unans-010',
        'What was the year-over-year percentage change in operating income?',
        'The accompanying notes describe the basis of preparation, '
        'including the adoption of recently issued accounting standards. '
        'All amounts are presented in thousands of US dollars unless '
        'otherwise indicated. Segment results include intersegment '
        'eliminations recorded in the corporate column.'
    ),
)
#: ``source_path`` recorded for every authored row, so the provenance of this
#: slice is visible in the data itself rather than only in the handoff.
UNANSWERABLE_SOURCE = "authored://t-v1-075/unanswerables"


def _unanswerable_rows() -> list[dict[str, Any]]:
    """Loader-shaped rows for the authored unanswerable slice."""
    return [
        {"id": sid, "question": q, "context": c, "gold": ""}
        for sid, q, c in UNANSWERABLE_ROWS
    ]


def _xquad_rows(lang: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    p = _DATASETS_ROOT / f"xquad-en-ro-v1/xquad.{lang}/validation-00000-of-00001.parquet"
    table = pq.read_table(p)
    rows: list[dict[str, Any]] = []
    for r in table.to_pylist():
        answers = r.get("answers", {}).get("text", [])
        gold = answers[0] if answers else ""
        ctx = r.get("context", "")
        q = r.get("question", "")
        if gold and ctx and q and score_faithfulness(gold, ctx) >= 0.90:
            rows.append(
                {
                    "id": f"xquad-{lang}-{r['id']}",
                    "question": q,
                    "context": ctx,
                    "gold": gold,
                }
            )
    return rows


def _pira_rows() -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    p = _DATASETS_ROOT / "pira-v1/test.parquet"
    table = pq.read_table(p)
    rows: list[dict[str, Any]] = []
    for r in table.to_pylist():
        q = r.get("question_pt_origin") or r.get("question_pt_paraphase") or ""
        a = r.get("answer_pt_origin") or r.get("answer_pt_validate") or ""
        ctx = r.get("abstract_translated_pt") or ""
        if q and a and ctx and score_faithfulness(a, ctx) >= 0.90:
            rows.append(
                {
                    "id": f"pira-pt-{r['id_qa']}",
                    "question": q,
                    "context": ctx,
                    "gold": a,
                }
            )
    return rows


def _tatdqa_rows() -> list[dict[str, Any]]:
    p = _DATASETS_ROOT / "t2-finance-tests-v1/data/TAT-DQA/test/metadata.jsonl"
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        q = item.get("question", "")
        gold = str(item.get("original_answer", ""))
        ctx = item.get("context", "")
        if q and gold and ctx and score_faithfulness(gold, ctx) >= 0.90:
            rows.append(
                {
                    "id": f"tatdqa-{item['id']}",
                    "question": q,
                    "context": ctx,
                    "gold": gold,
                }
            )
    return rows


def _hotpot_rows() -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    p = _DATASETS_ROOT / "ragbench-tests-v1/hotpotqa/test-00000-of-00001.parquet"
    table = pq.read_table(p)
    rows: list[dict[str, Any]] = []
    for r in table.to_pylist():
        docs = r.get("documents", [])
        ctx = "\n\n".join(docs) if isinstance(docs, list) else str(docs)
        resp = r.get("response", "")
        q = r.get("question", "")
        if q and ctx and resp and score_faithfulness(resp, ctx) >= 0.90:
            rows.append(
                {
                    "id": f"hotpot-{r['id']}",
                    "question": q,
                    "context": ctx,
                    "gold": resp,
                }
            )
    return rows


def build_slices(n_per_slice: int = DEFAULT_N_PER_SLICE) -> list[dict[str, Any]]:
    """Sample deterministic pinned records across the five Q8 surfaces."""
    loaders = {
        "en": lambda: _xquad_rows("en"),
        "ro": lambda: _xquad_rows("ro"),
        "pt": _pira_rows,
        "tables": _tatdqa_rows,
        "multi": _hotpot_rows,
        "unanswerable": _unanswerable_rows,
    }
    sources = {
        "en": "datasets/rag-eval/xquad-en-ro-v1/xquad.en/validation-00000-of-00001.parquet",
        "ro": "datasets/rag-eval/xquad-en-ro-v1/xquad.ro/validation-00000-of-00001.parquet",
        "pt": "datasets/rag-eval/pira-v1/test.parquet",
        "tables": "datasets/rag-eval/t2-finance-tests-v1/data/TAT-DQA/test/metadata.jsonl",
        "multi": "datasets/rag-eval/ragbench-tests-v1/hotpotqa/test-00000-of-00001.parquet",
        "unanswerable": UNANSWERABLE_SOURCE,
    }
    records: list[dict[str, Any]] = []
    for tag in SLICES:
        pool = loaders[tag]()
        rng = random.Random(SEEDS[tag])
        picks = rng.sample(pool, min(n_per_slice, len(pool)))
        for row in picks:
            records.append(
                {
                    "slice": tag,
                    "source_id": row["id"],
                    "source_path": sources[tag],
                    "question": row["question"],
                    "context": row["context"],
                    "gold_answer": row["gold"],
                    "content_sha256": _sha(row["question"], row["context"]),
                }
            )
    return records


class SliceIntegrityError(RuntimeError):
    """The pinned eval corpus is not the corpus the gates were measured on.

    Raised by :func:`verify_pinned_slices`. This exists so "frozen" is an
    enforced property rather than a claim: before T-V1-075 ``content_sha256``
    was *derived from* each record and never compared against anything, so a
    tampered ``gold_answer`` — which the per-record fingerprint does not even
    cover — would have been scored silently.
    """


#: T-V1-075 — the pin. Whole-file sha256 covers EVERY field of every record
#: (including ``gold_answer`` and ``source_id``, which ``content_sha256`` does
#: not), so any byte-level drift is detected. The per-slice counts are pinned
#: separately so a whole-file re-pin cannot quietly drop a slice.
PINNED_SLICES_SHA256 = "93d8f9562c96cf8dde1cfd19831e9de148423c77b053e3913ae872f53523998a"
PINNED_SLICE_COUNTS = {
    "en": 10,
    "ro": 10,
    "pt": 10,
    "tables": 10,
    "multi": 10,
    "unanswerable": 10,
}

#: How to re-pin deliberately, quoted in the error so the next person does not
#: "fix" a legitimate corpus change by deleting the check.
REPIN_INSTRUCTIONS = (
    "If this corpus change is intentional, re-pin it deliberately and record why: "
    "shasum -a 256 evals/q8_eval_slices.jsonl  -> update PINNED_SLICES_SHA256, and "
    "update PINNED_SLICE_COUNTS to match. Never edit the pin to make a failing "
    "check disappear."
)


def verify_pinned_slices(target: Path, raw: str, records: list[dict[str, Any]]) -> None:
    """Fail loudly unless ``raw`` is byte-identical to the pinned corpus.

    Three independent checks, cheapest first:

    1. whole-file sha256 — catches a drift in ANY field of ANY record;
    2. per-slice record counts — catches a silently dropped or duplicated slice
       even if someone re-pins the hash without thinking;
    3. per-record ``content_sha256`` — catches a hand-edited record whose stored
       fingerprint was not recomputed.

    Raises :class:`SliceIntegrityError` on any mismatch.
    """
    digest = hashlib.sha256(raw.encode()).hexdigest()
    if digest != PINNED_SLICES_SHA256:
        raise SliceIntegrityError(
            f"{target} is not the pinned corpus: sha256 {digest} != {PINNED_SLICES_SHA256}. "
            f"Corpus drift changes every benchmark number. " + REPIN_INSTRUCTIONS
        )

    counts: dict[str, int] = {}
    for rec in records:
        counts[rec.get("slice", "?")] = counts.get(rec.get("slice", "?"), 0) + 1
    if counts != PINNED_SLICE_COUNTS:
        raise SliceIntegrityError(
            f"{target} slice counts {counts} != pinned {PINNED_SLICE_COUNTS}. " + REPIN_INSTRUCTIONS
        )

    for rec in records:
        expected = _sha(rec.get("question", ""), rec.get("context", ""))
        stored = rec.get("content_sha256")
        if stored != expected:
            raise SliceIntegrityError(
                f"record {rec.get('source_id')!r}: content_sha256 {stored!r} != "
                f"recomputed {expected!r} — the record was edited without recomputing "
                "its fingerprint. " + REPIN_INSTRUCTIONS
            )


def load_slices(path: Path | None = None) -> list[dict[str, Any]]:
    """Load a pinned slice file back (shared by the Q9 runner/tests).

    T-V1-075: when the pinned default corpus is loaded, its integrity is
    ENFORCED here — not by a standalone script nobody calls — so every consumer
    of the benchmark data (the Q9 runner included) fails loudly on drift. An
    explicit non-default ``path`` is a deliberate alternative corpus and is
    returned unverified.
    """
    target = path or DEFAULT_OUT
    raw = target.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if target == DEFAULT_OUT:
        verify_pinned_slices(target, raw, records)
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Q8 pinned eval slices")
    parser.add_argument("--n-per-slice", type=int, default=DEFAULT_N_PER_SLICE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    records = build_slices(args.n_per_slice)
    args.out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    counts = {tag: sum(1 for r in records if r["slice"] == tag) for tag in SLICES}
    print(f"wrote {len(records)} records -> {args.out} {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
