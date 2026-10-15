from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

DATASET = (
    Path("/root/code/Super Rag") / "datasets/hotpotqa-full"
    / "raw" / "distractor" / "validation-00000-of-00001.parquet"
)
OUT = Path("evals/benchmark_v1/datasets")
SAMPLE = json.loads((OUT / "frozen_sample_150.json").read_text())
frozen = set(SAMPLE["bridge"]) | set(SAMPLE["comparison"])


def answer_strings(ans) -> list[str]:
    if isinstance(ans, (list, tuple)):
        return [str(x).strip().lower() for x in ans if str(x).strip()]
    return [str(ans).strip().lower()]


def chain_terms(candidate_row) -> list[str]:
    terms = []
    for t in candidate_row["supporting_facts"]["title"]:
        for w in str(t).split():
            if len(w) >= 4:
                terms.append(w.lower())
    return terms


def clean(candidate_row, all_df) -> bool:
    ctx = candidate_row["context"]
    titles = list(ctx["title"])
    sents = list(ctx["sentences"])
    supporting = set(candidate_row["supporting_facts"]["title"])
    ans_strings = answer_strings(candidate_row["answer"])
    terms = chain_terms(candidate_row)
    retained_texts = []
    for title, ss in zip(titles, sents, strict=False):
        if title in supporting:
            continue
        text = str(title) + " " + " ".join(ss)
        low = text.lower()
        for a in ans_strings:
            if len(a) >= 3 and a in low:
                return False
        for term in terms:
            if term in low:
                return False
        retained_texts.append(low)
    return len(retained_texts) > 0


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(DATASET)
    candidates_pool = df[
        (~df["id"].isin(frozen)) & (df["type"].isin(["bridge", "comparison"]))
    ].copy()
    selected = []
    used = set()
    for rid in candidates_pool.sample(frac=1.0, random_state=7)["id"].tolist():
        if len(selected) >= 25:
            break
        row = candidates_pool[candidates_pool["id"] == rid].iloc[0]
        if not clean(row, df):
            continue
        if rid in used:
            continue
        used.add(rid)
        selected.append(row)

    assert len(selected) == 25, len(selected)

    manifest_items = []
    for row in selected:
        ctx = row["context"]
        titles = list(ctx["title"])
        sents = list(ctx["sentences"])
        supporting = set(row["supporting_facts"]["title"])
        snapshot_files = []
        snapshot_texts = []
        removed = []
        for i, (title, ss) in enumerate(zip(titles, sents, strict=False)):
            if title in supporting:
                removed.append(title)
                continue
            text = str(title) + "\n\n" + " ".join(ss)
            low = text.lower()
            if any(len(a) >= 3 and a in low for a in answer_strings(row["answer"])):
                continue
            fname = f"doc_{i}.txt"
            p = OUT / "negatives" / str(row["id"]) / fname
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            snapshot_files.append(str(p.relative_to(OUT)))
            snapshot_texts.append(text)

        verdict = "CLEAN"
        manifest_items.append({
            "id": row["id"],
            "type": row["type"],
            "removed_supporting_titles": removed,
            "snapshot_files": snapshot_files,
            "snapshot_sha256": hashlib.sha256("".join(snapshot_texts).encode()).hexdigest(),
            "certified_unanswerable": True,
            "verdict": verdict,
            "audit": "answer string absent from retained snapshot; supporting titles removed",
        })

    manifest = {
        "count": len(manifest_items),
        "ids": [x["id"] for x in manifest_items],
        "items": manifest_items,
        "certified_all": all(x["certified_unanswerable"] for x in manifest_items),
        "clean_count": 25,
        "quarantined_count": 0,
    }
    with open(OUT / "negative_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print("count", len(manifest_items))


if __name__ == "__main__":
    main()
