"""Build the HotpotQA distractor validation corpus manifest.

Run from the knowledge-os repository root:
    python evals/benchmark_v1/scripts/build_corpus.py
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

import pandas as pd

DATASET = (
    Path("/root/code/Super Rag") / "datasets/hotpotqa-full"
    / "raw" / "distractor" / "validation-00000-of-00001.parquet"
)
OUT_DIR = Path("evals/benchmark_v1/datasets")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(DATASET)
    random.seed(42)

    seen: dict[str, int] = {}
    unique_count = 0
    collisions = 0
    raw_count = 0
    title_count: set[str] = set()

    for _, row in df.iterrows():
        ctx = row["context"]
        titles = list(ctx["title"])
        sentences = list(ctx["sentences"])
        for title, sents in zip(titles, sentences, strict=False):
            chunks = sents if isinstance(sents, (list, tuple)) else [sents]
            for paragraph in chunks:
                raw_count += 1
                text = str(paragraph).strip()
                norm = " ".join(text.split()).lower()
                title_norm = str(title).strip().lower()
                key = (title_norm, norm)
                title_count.add(str(title).strip())
                if key in seen:
                    collisions += 1
                else:
                    seen[key] = 1
                    unique_count += 1

    # canonical id list of hashes only (no text)

    manifest = {
        "dataset": "hotpotqa",
        "split": "distractor_validation",
        "dedup_rule": "normalized (lowercased trimmed title + whitespace-collapsed paragraph)",
        "raw_placements": raw_count,
        "unique_paragraphs": unique_count,
        "collisions": collisions,
        "unique_titles": len(title_count),
        "content_sha256": hashlib.sha256(
            json.dumps(sorted(seen), sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }
    with (OUT_DIR / "corpus_manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # frozen sample: 75 bridge / 75 comparison
    bridge_ids = df[df["type"] == "bridge"].sample(n=75, random_state=42)["id"].tolist()
    comparison_ids = df[df["type"] == "comparison"].sample(n=75, random_state=42)["id"].tolist()
    sample = {
        "seed": 42,
        "bridge": bridge_ids,
        "comparison": comparison_ids,
        "n": 150,
        "dataset": "hotpotqa",
    }
    with (OUT_DIR / "frozen_sample_150.json").open("w", encoding="utf-8") as f:
        json.dump(sample, f, indent=2)

    import os
    print("raw", raw_count, "unique", unique_count, "collisions", collisions)
    print("manifest_bytes", os.path.getsize(OUT_DIR / "corpus_manifest.json"))


if __name__ == "__main__":
    main()
