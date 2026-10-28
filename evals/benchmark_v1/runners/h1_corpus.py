"""H1 closed-context corpus assembly (no index, no embedding store).

Each item's context is its own gold distractor paragraphs, read straight from the
local HotpotQA distractor parquet (or the local 2wiki doc files / certified
negative snapshots). Nothing is retrieved and nothing is indexed: H1 is a
**closed-context reader A/B**, so the retrieved-passage text is exactly the
paragraph set handed to the reader — which is also what makes the captured trace
replayable (T-P01's TRACE-GAP).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

_REPO = pathlib.Path(__file__).resolve().parents[3]
BENCH = _REPO / "evals" / "benchmark_v1"
HOTPOT_PARQUET = (
    pathlib.Path("/root/code/Super Rag/datasets/hotpotqa-full")
    / "raw" / "distractor" / "validation-00000-of-00001.parquet"
)
TWO_WIKI_MANIFEST = _REPO / "evals" / "ingest" / "manifest.json"
TWO_WIKI_DOCS = _REPO / "evals" / "ingest" / "docs"
NEGATIVE_MANIFEST = BENCH / "datasets" / "negative_manifest.json"
NEGATIVE_DIR = BENCH / "datasets" / "negatives"

#: R repeats, frozen by the preregistration (R=2 exactly for HotpotQA).
R_HOTPOT = 2
R_TWOWIKI = 1
R_NEGATIVE = 1


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_hotpot() -> list[dict[str, Any]]:
    """Frozen 150 HotpotQA distractor items with their 10 gold paragraphs."""
    import pyarrow.parquet as pq

    frozen = json.loads((BENCH / "datasets" / "frozen_sample_150.json").read_text())
    want = list(frozen["bridge"]) + list(frozen["comparison"])
    table = pq.read_table(HOTPOT_PARQUET)
    cols = table.column_names
    by_id = {
        row["id"]: row
        for row in table.select(cols).to_pylist()
        if row["id"] in set(want)
    }
    missing = [q for q in want if q not in by_id]
    if missing:
        raise RuntimeError(f"{len(missing)} frozen ids absent from the parquet")

    items: list[dict[str, Any]] = []
    for qid in want:
        row = by_id[qid]
        titles = list(row["context"]["title"])
        sentences = list(row["context"]["sentences"])
        paragraphs = [
            {"title": t, "text": " ".join(s)}
            for t, s in zip(titles, sentences, strict=True)
        ]
        gold_titles = set(row["supporting_facts"]["title"])
        items.append(
            {
                "item_id": qid,
                "dataset": "hotpotqa-distractor",
                "question": row["question"],
                "gold_answer": row["answer"],
                "gold_titles": sorted(gold_titles),
                "gold_supporting_facts": [
                    [t, str(i)] for t, i in zip(
                        row["supporting_facts"]["title"],
                        row["supporting_facts"]["sent_id"],
                        strict=True,
                    )
                ],
                "paragraphs": paragraphs,
                "context_sha256": _sha("".join(p["text"] for p in paragraphs)),
                "question_sha256": _sha(row["question"]),
                "answerable": True,
                "repeats": R_HOTPOT,
            }
        )
    return items


def load_two_wiki() -> list[dict[str, Any]]:
    """48 non-slice 2wiki items, one draft each."""
    manifest = json.loads(TWO_WIKI_MANIFEST.read_text())
    items: list[dict[str, Any]] = []
    for row in manifest["items"]:
        paragraphs = []
        for doc_id in row["all_doc_ids"]:
            p = TWO_WIKI_DOCS / f"{doc_id}.txt"
            if p.exists():
                paragraphs.append({"title": doc_id, "text": p.read_text(encoding="utf-8").strip()})
        items.append(
            {
                "item_id": row["id"],
                "dataset": "2wikimultihopqa",
                "question": row["question"],
                "gold_answer": row.get("expected_answer", ""),
                "gold_titles": list(row.get("gold_doc_ids", [])),
                "gold_supporting_facts": [],
                "paragraphs": paragraphs,
                "context_sha256": _sha("".join(p["text"] for p in paragraphs)),
                "question_sha256": _sha(row["question"]),
                "answerable": True,
                "repeats": R_TWOWIKI,
                "question_type": row.get("type", ""),
            }
        )
    return items


def load_negatives() -> list[dict[str, Any]]:
    """25 certified-unanswerable items; their snapshot FILES are the context."""
    manifest = json.loads(NEGATIVE_MANIFEST.read_text())
    items: list[dict[str, Any]] = []
    for row in manifest["items"]:
        paragraphs = []
        for rel in row["snapshot_files"]:
            p = BENCH / "datasets" / rel
            if p.exists():
                paragraphs.append({"title": p.stem, "text": p.read_text(encoding="utf-8").strip()})
        items.append(
            {
                "item_id": row["id"],
                "dataset": "certified-negative",
                "question": "",
                "gold_answer": "",
                "gold_titles": [],
                "gold_supporting_facts": [],
                "paragraphs": paragraphs,
                "context_sha256": _sha("".join(p["text"] for p in paragraphs)),
                "question_sha256": _sha(row["id"]),
                "answerable": False,
                "repeats": R_NEGATIVE,
                "snapshot_sha256": row.get("snapshot_sha256", ""),
                "certified_unanswerable": bool(row.get("certified_unanswerable", True)),
                "verdict": row.get("verdict", ""),
                "audit": row.get("audit", ""),
                "question_type": row.get("type", ""),
            }
        )
    return items


def load_all() -> dict[str, list[dict[str, Any]]]:
    """The three H1 slices, keyed for the runner."""
    hotpot = load_hotpot()
    twowiki = load_two_wiki()
    negatives = load_negatives()
    # The negative manifest IDs originate from HotpotQA distractor validation parquet.
    import pyarrow.parquet as pq
    hp_table = pq.read_table(HOTPOT_PARQUET, columns=["id", "question"])
    hp_questions = {
        r["id"]: r["question"]
        for r in hp_table.to_pylist()
        if r["id"] in {n["item_id"] for n in negatives}
    }
    twowiki_questions = {i["item_id"]: i["question"] for i in twowiki}
    for n in negatives:
        n["question"] = hp_questions.get(n["item_id"], twowiki_questions.get(n["item_id"], ""))
        n["question_sha256"] = _sha(n["question"])
    return {"hotpot": hotpot, "twowiki": twowiki, "negatives": negatives}


def expected_attempts() -> dict[str, int]:
    """Attempt counts the runner must account for (failures stay in)."""
    hotpot = len(load_hotpot())
    twowiki = len(load_two_wiki())
    neg = len(load_negatives())
    return {
        "hotpot_items": hotpot,
        "hotpot_attempts": hotpot * R_HOTPOT,
        "twowiki_items": twowiki,
        "twowiki_attempts": twowiki * R_TWOWIKI,
        "negative_items": neg,
        "negative_attempts": neg * R_NEGATIVE,
        "total_attempts": hotpot * R_HOTPOT + twowiki * R_TWOWIKI + neg * R_NEGATIVE,
    }