"""Build the G5 reproducibility manifest + source inventory (NOT product code).

Standard-library only. Walks the pinned dataset roots, hashes every file
(SHA-256), groups files into **source families** (the leakage unit for the
dev/holdout split), and writes a manifest JSON plus an honest notes list. It
records missing annotations (no gold chunk ids, no dev/holdout split in the
upstream data, parquet schemas unread without a dependency) rather than
inventing labels or parsing formats it cannot read.

Run (from repo root):

    /root/ko-venv/bin/python3 evals/build_graph_manifest.py \
        --datasets-root /root/code/Super\\ Rag/datasets/rag-eval \
        --out docs/graph/G5-DATASET-MANIFEST.json

What it does NOT do: no ingestion, no service calls, no model calls, no label
synthesis, no holdout tuning. It only inventories bytes + hashes present on disk.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "packages", _REPO, _REPO / "tests" / "fixtures"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from graph_dataset_manifest import (  # noqa: E402 - must follow sys.path insert
    SourceInventory,
    build_inventory,
)


def _finqa_family(rel: str) -> str:
    """FinQA family = company + report year (a report and its questions).

    Path shape: ``t2-finance-tests-v1/data/FinQA/test/pdf/<SYMBOL>/<YEAR>/<file>``
    for sources; the split-level ``metadata.jsonl`` is one shared annotation file
    (its own family). Two different years of the same company are DIFFERENT
    families (distinct reports); the same company+year is ONE family.
    """
    parts = rel.split("/")
    if "pdf" in parts:
        i = parts.index("pdf")
        if i + 2 < len(parts):
            return f"finqa:{parts[i + 1]}:{parts[i + 2]}"
    if rel.endswith("metadata.jsonl"):
        return "finqa:metadata"
    return "finqa:other"


def _tatdqa_family(rel: str) -> str:
    """TAT-DQA family = the raw report (filename stem) + shared metadata.

    TAT-DQA raw files are hashed ids; without parsing we cannot map each raw
    file to a company, so per-file families are keyed by filename stem and the
    metadata.jsonl is its own family. Intentionally conservative: never merges
    two files into one family on a guess.
    """
    if rel.endswith("metadata.jsonl"):
        return "tatdqa:metadata"
    return f"tatdqa:{Path(rel).stem}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build G5 dataset manifest + inventory")
    ap.add_argument("--datasets-root", required=True)
    ap.add_argument("--out", required=True, help="manifest JSON output path")
    ap.add_argument(
        "--include-suffixes",
        default="pdf,png,json,jsonl,csv,parquet,md,txt",
        help="comma-separated file suffixes to hash",
    )
    args = ap.parse_args(argv)

    root = Path(args.datasets_root)
    suffixes = {
        s.strip().lstrip(".").lower() for s in args.include_suffixes.split(",") if s.strip()
    }

    finqa = root / "t2-finance-tests-v1" / "data" / "FinQA"
    tatdqa = root / "t2-finance-tests-v1" / "data" / "TAT-DQA"

    families = {}
    if finqa.exists():
        inv = build_inventory(
            finqa, family_fn=_finqa_family, include_suffixes=suffixes, common_root=root
        )
        families.update(inv.families)
    if tatdqa.exists():
        inv = build_inventory(
            tatdqa, family_fn=_tatdqa_family, include_suffixes=suffixes, common_root=root
        )
        families.update(inv.families)

    notes = [
        "FinQA/TAT-DQA upstream ships a single split='test' with NO dev/holdout "
        "partition and NO gold chunk ids; 'context' is pre-extracted text, not the "
        "chunked corpus. Gold chunk/source refs and a family split must be authored "
        "before a real paired G5 eval (see docs/graph/G5-EVAL-REPORT.md).",
        "Parquet corpora (ragbench-tests hotpotqa/tatqa/finqa, enterprise-rag-bench, "
        "financebench merged, xquad) are hashed by bytes but NOT parsed here: no "
        "pyarrow/fastparquet in the test venv and adding a dependency is out of scope. "
        "Their schemas are reported from their README/manifest, never guessed.",
        "financebench-noncommercial-v1 is NON-COMMERCIAL licensed; do not use for a "
        "released product eval without a license review. enterprise-rag-bench-v1 is MIT "
        "and manifest-pinned (HF rev 69916e31...); its questions/gold are eval-only.",
    ]

    inventory = SourceInventory(root=str(root), families=families, notes=notes)
    manifest = inventory.to_manifest()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest.write(out)

    print(f"wrote {out}")
    print(f"files={len(manifest.files)} total_bytes={sum(f.size_bytes for f in manifest.files)}")
    print(f"families={len(set(manifest.family_of.values()))}")
    for n in notes:
        print(f"NOTE: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
