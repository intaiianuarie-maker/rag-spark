"""Build Q8 pinned evaluation slices across EN, RO, PT, tables, and multi-source.

Pins verified extractive evaluation test slices where answers are supported
by context:
- EN: xquad-en-ro-v1 (xquad.en)
- RO: xquad-en-ro-v1 (xquad.ro)
- PT: pira-v1 (Portuguese QA with context-substantiated answers)
- tables: t2-finance-tests-v1 (TAT-DQA extractive table QA)
- multi: ragbench-tests-v1 (hotpotqa multi-source)

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

SLICES = ("en", "ro", "pt", "tables", "multi")
SEEDS = {
    "en": 20261012,
    "ro": 20261013,
    "pt": 20261014,
    "tables": 20261015,
    "multi": 20261016,
}


def _sha(q: str, c: str) -> str:
    return hashlib.sha256(f"{q}\n{c}".encode()).hexdigest()[:16]


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
    }
    sources = {
        "en": "datasets/rag-eval/xquad-en-ro-v1/xquad.en/validation-00000-of-00001.parquet",
        "ro": "datasets/rag-eval/xquad-en-ro-v1/xquad.ro/validation-00000-of-00001.parquet",
        "pt": "datasets/rag-eval/pira-v1/test.parquet",
        "tables": "datasets/rag-eval/t2-finance-tests-v1/data/TAT-DQA/test/metadata.jsonl",
        "multi": "datasets/rag-eval/ragbench-tests-v1/hotpotqa/test-00000-of-00001.parquet",
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


def load_slices(path: Path | None = None) -> list[dict[str, Any]]:
    """Load a pinned slice file back (shared by the Q9 runner/tests)."""
    target = path or DEFAULT_OUT
    return [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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
