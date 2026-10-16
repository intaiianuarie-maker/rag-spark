"""H2 corpus: canonical index reconstruction + frozen H2 questions + retrieval.

H2 is an *open-context* reader A/B run on TOP of the index built by
``index/build_index.py``. To retrieve from that index we must line vector row
``i`` up with the canonical paragraph ``i`` used at build time. This module
reconstructs that ordering **verbatim** (same dedup loop as ``build_index.py``)
and cross-checks it against ``index/ids.json`` so a misaligned index fails loud
instead of silently retrieving the wrong paragraphs.

Nothing in this module makes a model call. Query embedding + metering live in
``h2_run.py``; this module is a pure, offline data layer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

_REPO = Path(__file__).resolve().parents[3]
BENCH = _REPO / "evals" / "benchmark_v1"

#: HotpotQA distractor validation parquet -- identical source path to
#: index/build_index.py and evals/benchmark_v1/runners/h1_corpus.py.
HOTPOT_PARQUET = (
    Path("/root/code/Super Rag")
    / "datasets"
    / "hotpotqa-full"
    / "raw"
    / "distractor"
    / "validation-00000-of-00001.parquet"
)
FROZEN_150 = BENCH / "datasets" / "frozen_sample_150.json"

INDEX_DIR = BENCH / "index"
VECTORS_NPY = INDEX_DIR / "vectors.npy"
IDS_JSON = INDEX_DIR / "ids.json"
INDEX_MANIFEST = INDEX_DIR / "index_manifest.json"

#: H2 retrieval depth (matches the ticket's @10).
K = 10
EXPECTED_COUNT = 66635
EMBED_DIM = 3072


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_frozen_items() -> list[dict[str, Any]]:
    """The 150 held-out HotpotQA items (bridge 75 + comparison 75).

    Mirrors ``h1_corpus.load_hotpot`` shape so traces are comparable, but the
    ``paragraphs`` field is intentionally left empty here: H2 fills it from
    retrieval rather than from the gold distractor set.
    """
    import pandas as pd

    frozen = json.loads(FROZEN_150.read_text(encoding="utf-8"))
    df = pd.read_parquet(HOTPOT_PARQUET).set_index("id")
    items: list[dict[str, Any]] = []
    for slice_name in ("bridge", "comparison"):
        for qid in frozen[slice_name]:
            row = df.loc[qid]
            gold_titles = sorted(set(row["supporting_facts"]["title"]))
            q = str(row["question"])
            items.append(
                {
                    "item_id": str(qid),
                    "dataset": "hotpotqa-distractor",
                    "split": "validation",
                    "slice": slice_name,
                    # 1 repeat for the H2 pilot (closed protocol).
                    "repeats": 1,
                    "question": q,
                    "question_sha256": _sha(q),
                    "answer": str(row["answer"]),
                    "gold_titles": gold_titles,
                    "gold_supporting_facts": [
                        [t, str(s)]
                        for t, s in zip(
                            row["supporting_facts"]["title"],
                            row["supporting_facts"]["sent_id"],
                            strict=True,
                        )
                    ],
                    "paragraphs": [],  # filled by retrieval (see h2_run.py)
                    "context_sha256": "",
                    "answerable": True,
                    "n_supporting_titles": len(gold_titles),
                }
            )
    return items


def build_canonical_corpus() -> list[dict[str, str]]:
    """Reconstruct the 66635 canonical paragraphs in ``build_index.py`` order.

    This is the EXACT dedup loop from ``index/build_index.py`` (one canonical
    paragraph per unique (title, sentence-array) pair, first-seen order across
    the parquet). We then assert the resulting title list equals
    ``index/ids.json`` -- build_index.py's own output -- so a drift in the
    reconstruction is impossible to miss.
    """
    import pandas as pd

    df = pd.read_parquet(HOTPOT_PARQUET)
    seen: dict[tuple[str, str], int] = {}
    order: list[dict[str, str]] = []
    for _, row in df.iterrows():
        ctx = row["context"]
        titles = list(ctx["title"])
        sents = list(ctx["sentences"])
        for title, sents_ in zip(titles, sents, strict=False):
            for p in sents_ if isinstance(sents_, (list, tuple)) else [sents_]:
                text = str(p).strip()
                norm = " ".join(text.split()).lower()
                title_norm = str(title).strip().lower()
                key = (title_norm, norm)
                if key not in seen:
                    seen[key] = len(order)
                    order.append({"title": str(title).strip(), "text": text})
    if len(order) != EXPECTED_COUNT:
        raise RuntimeError(
            f"canonical corpus count {len(order)} != {EXPECTED_COUNT}"
        )
    saved = json.loads(IDS_JSON.read_text(encoding="utf-8"))
    if [o["title"] for o in order] != saved:
        raise RuntimeError(
            "canonical corpus order != index/ids.json (vector index misalignment)"
        )
    return order


def load_vectors() -> np.ndarray:
    """Pre-built, unit-normalized paragraph embeddings (66635 x 3072, float32)."""
    vecs = np.load(VECTORS_NPY, allow_pickle=False)
    if vecs.shape != (EXPECTED_COUNT, EMBED_DIM):
        raise RuntimeError(
            f"vectors.npy shape {vecs.shape} != ({EXPECTED_COUNT}, {EMBED_DIM})"
        )
    if not np.isfinite(vecs).all():
        raise RuntimeError("vectors.npy contains non-finite values")
    # The index is built normalized; defensively re-normalize so a tiny float32
    # drift never turns a cosine into a scaled dot product.
    norms = np.linalg.norm(vecs, axis=1)
    if float(norms.min()) < 0.999 or float(norms.max()) > 1.001:
        vecs = vecs / np.maximum(norms[:, None], 1e-12)
    return np.ascontiguousarray(vecs, dtype=np.float32)


def _normalize(vec: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(vec))
    return vec / n if n > 0.0 else vec


def retrieve(
    qvec: list[float] | np.ndarray,
    vectors: np.ndarray,
    corpus: list[dict[str, str]],
    k: int = K,
) -> tuple[list[dict[str, str]], list[int], list[float]]:
    """Top-K cosine retrieval via matrix multiplication: ``scores = vectors @ q``.

    ``vectors`` is unit-normalized (see :func:`load_vectors`) and ``qvec`` is
    L2-normalized here, so the dot product is exact cosine similarity.
    """
    q = np.asarray(qvec, dtype=np.float32).ravel()
    qn = _normalize(q)
    if qn.shape[0] != vectors.shape[1]:
        raise RuntimeError(
            f"query dim {qn.shape[0]} != index dim {vectors.shape[1]}"
        )
    scores = vectors @ qn  # cosine, shape (66635,)
    k = min(k, vectors.shape[0])
    top = np.argpartition(scores, -k)[-k:]
    # descending within the top-k slice
    top = top[np.argsort(scores[top])[::-1]]
    paragraphs = [corpus[int(i)] for i in top]
    return (
        paragraphs,
        [int(i) for i in top],
        [round(float(s), 6) for s in scores[top]],
    )


def evidence_recall_at_10(
    gold_titles: list[str], retrieved_titles: list[str]
) -> float:
    """Title-level recall@10 of gold supporting-fact titles in the retrieved set.

    Per the ticket: measured against ``row['supporting_facts']['title']``.
    """
    if not gold_titles:
        return 1.0  # vacuously recovered -- nothing to find
    gold = set(gold_titles)
    retrieved = set(retrieved_titles)
    return round(len(gold & retrieved) / len(gold), 6)

