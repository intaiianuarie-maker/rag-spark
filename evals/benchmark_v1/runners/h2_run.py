"""H2 end-to-end reader A/B over the built HotpotQA index (metered, in-cap).

  1. Questions: 150 held-out HotpotQA distractor items from
     datasets/frozen_sample_150.json (bridge 75 + comparison 75).
  2. Index: index/vectors.npy (66635 x 3072, normalized). The canonical
     paragraph order is reconstructed verbatim in h2_corpus (asserted == ids.json)
     so vector row i lines up with corpus[i].
  3. Retrieval: per item the query is embedded once with OpenRouterProvider
     (call_site='h2_query_embed', gemini-embedding-2, 3072-d); top-K cosine
     retrieval is a matrix multiply scores = vectors @ q; EvidenceRecall@10 is
     measured against supporting_facts['title'].
  4. Reader: ONE shared first draft per item (DRAFT_PROMPT_CAPPED from h1_run.py)
     fed to OFF -> ExtractiveGrounder and ON -> ParaphraseTolerantGrounder.
     Draft + OFF/ON + bounded repair are H1's exact run_attempt, reused verbatim
     with item['paragraphs'] = the retrieved chunks.
  5. Safety: a pre-call arithmetic gate reads the SHARED budget_ledger.csv
     (metered spend + sunk corpus-index cost) and refuses to start if the
     worst-case H2 cost exceeds the remaining 2.00 USD wave cap. Every metered
     call appends to budget_ledger.csv under run_tag 'h2_retrieval_ab'.

Outputs: runs/h2/{traces.jsonl, predictions.jsonl, run_summary.json},
reports/h2_pre_call_arithmetic.json.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import json
import pathlib
import sys
import time
from typing import Any

_REPO = pathlib.Path(__file__).resolve().parents[3]
for _p in (_REPO / "packages", _REPO / "apps" / "api", _REPO / "apps" / "worker",
           _REPO / "evals" / "vendor", str(_REPO / "evals" / "benchmark_v1" / "runners")):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from h1_run import (  # noqa: E402
    DRAFT_PROMPT_CAPPED,
    LEDGER,
    MAX_REPAIRS_PER_ARM,
    TOTAL_CAP_USD,
    WORST_UNIT_USD,
    Harness,
    _delta,
    _sha,
    _snapshot,
    run_attempt,
    spent_to_date,
    write_ledger,
)
from h2_corpus import (  # noqa: E402
    K,
    build_canonical_corpus,
    evidence_recall_at_10,
    load_frozen_items,
    load_vectors,
    retrieve,
)
from models.adapters.openrouter_provider import (  # noqa: E402
    DEFAULT_EMBEDDING_MODEL,
    OpenRouterProvider,
)

RUN_TAG = "h2_retrieval_ab"
RUNS = _REPO / "evals" / "benchmark_v1" / "runs" / "h2"
ARITH = _REPO / "evals" / "benchmark_v1" / "reports" / "h2_pre_call_arithmetic.json"
#: Conservative worst-case per query embed (real ~4-9e-6; bound it at 1e-5).
WORST_EMBED_UNIT = 1.0e-5


def index_sunk_usd() -> float:
    """Corpus-index cost already realized, as recorded in budget_ledger.csv.

    build_index.py wrote the 66.6k-vector index as cost_projection checkpoints
    (run_tag='h2-index'); the final cumulative row is the total sunk cost. We
    read it from the ledger so H2's gate accounts for the already-built index
    and stays genuinely in-cap against the 2.00 USD wave budget.
    """
    if not LEDGER.exists():
        return 0.0
    sunk = 0.0
    with LEDGER.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("run_tag") == "h2-index"
                    and row.get("row_type") == "cost_projection"):
                try:
                    sunk = max(sunk, float(row.get("cost_usd") or 0))
                except ValueError:
                    continue
    return sunk


def remaining_cap() -> float:
    """Live in-cap runtime headroom read from budget_ledger.csv."""
    return round(TOTAL_CAP_USD - spent_to_date() - index_sunk_usd(), 6)


def pre_call_arithmetic(n_attempts: int, run_tag: str = RUN_TAG) -> dict[str, object]:
    """H2 worst-case cost model, computed BEFORE the first metered call.

    Per item: 1 query embed + 1 shared draft + 2 arms x MAX_REPAIRS_PER_ARM
    repair drafts. The embed is priced conservatively at WORST_EMBED_UNIT; every
    generation leg at the paid-generation worst unit WORST_UNIT_USD (P02 measured:
    16/16 free-tier fallbacks, so no free savings assumed).
    """
    n = n_attempts if n_attempts > 0 else 0
    units_per_attempt = 1 + 1 + 2 * MAX_REPAIRS_PER_ARM  # embed + draft + 2 arms
    worst_per_attempt = WORST_EMBED_UNIT + (1 + 2 * MAX_REPAIRS_PER_ARM) * WORST_UNIT_USD
    worst_total = worst_per_attempt * n
    metered = spent_to_date()
    index = index_sunk_usd()
    remaining = round(TOTAL_CAP_USD - metered - index, 6)
    out: dict[str, object] = {
        "run_tag": run_tag,
        "computed_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "n_attempts": n,
        "query_embeds": n,
        "shared_drafts": n,
        "arms": 2,
        "max_repairs_per_arm": MAX_REPAIRS_PER_ARM,
        "worst_case_repairs": 2 * MAX_REPAIRS_PER_ARM * n,
        "metered_units_per_attempt": units_per_attempt,
        "worst_embed_unit_usd": WORST_EMBED_UNIT,
        "worst_gen_unit_usd": WORST_UNIT_USD,
        "worst_per_attempt_usd": round(worst_per_attempt, 8),
        "worst_total_usd": round(worst_total, 6),
        "wave_cap_usd": TOTAL_CAP_USD,
        "metered_spent_usd": round(metered, 8),
        "index_sunk_usd": round(index, 6),
        "remaining_cap_usd": remaining,
        "note": (
            "Worst case prices every generation leg at the paid-generation worst "
            "unit (P02 measured) and the per-query embed at a conservative 1e-5. "
            "The 66.6k corpus index is already built and recorded in "
            "budget_ledger.csv (cost_projection rows, run_tag h2-index) and is "
            "subtracted as sunk, so remaining_cap_usd is the real in-cap runtime "
            "headroom against the 2.00 USD wave budget."
        ),
    }
    if worst_total >= remaining:
        out["verdict"] = "BUDGET-GATE"
        out["reason"] = (
            f"worst case ${worst_total:.6f} >= remaining ${remaining:.6f}; not starting"
        )
    else:
        out["verdict"] = "PROCEED"
    return out


class H2Harness(Harness):
    """H2 harness: retrieval-indexed reader A/B over the built index.

    Reuses H1's shared-draft + OFF/ON grounder + bounded repair (run_attempt);
    only the query-embedding leg is added here, metered under 'h2_query_embed'.
    """

    def __init__(self, run_tag: str = RUN_TAG) -> None:
        super().__init__(run_tag=run_tag)
        # Per the protocol, query embedding is metered under its own call site.
        self.provider = OpenRouterProvider(call_site="h2_query_embed")

    async def embed_query(
        self, text: str, site: str = "h2_query_embed"
    ) -> tuple[list[float], str, float]:
        """Embed a query, appending one metered_call ledger row (T-P02)."""
        before = _snapshot()
        t0 = time.monotonic()
        err = ""
        vec: list[float] = []
        try:
            raw = await self.provider.embed_query(text, call_site=site)
            vec = [float(x) for x in raw]
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:200]
        d = _delta(before, _snapshot())
        latency = int((time.monotonic() - t0) * 1000)
        self.ledger_rows.append({
            "timestamp_utc": datetime.datetime.now(datetime.UTC).isoformat(),
            "run_tag": self.run_tag,
            "row_type": "metered_call",
            "call_site": site,
            "model_requested": DEFAULT_EMBEDDING_MODEL,
            "model_used": DEFAULT_EMBEDDING_MODEL,
            "calls": int(d["calls"]),
            "free_calls": int(d["free_calls"]),
            "paid_calls": int(d["paid_calls"]),
            "fallbacks": int(d["fallbacks"]),
            "failed_calls": int(d["failed_calls"]),
            "prompt_tokens": int(d["prompt_tokens"]),
            "completion_tokens": int(d["completion_tokens"]),
            "cost_usd": d["cost_usd"],
            "latency_ms": latency,
            "error": err,
            "detail": f"dim={len(vec)}",
        })
        return vec, err, float(d["cost_usd"])


async def run_attempt_h2(
    h: H2Harness,
    item: dict,
    vectors: Any,
    corpus: list[dict[str, str]],
    k: int = K,
) -> tuple[dict, float]:
    """Embed the question, retrieve top-k, then run H1's shared-draft A/B."""
    vec, embed_err, embed_cost = await h.embed_query(
        item["question"], site="h2_query_embed"
    )
    if vec:
        retrieved, topk_idx, scores = retrieve(vec, vectors, corpus, k)
    else:
        retrieved, topk_idx, scores = [], [], []
    retrieved_titles = [p["title"] for p in retrieved]
    recall = evidence_recall_at_10(item["gold_titles"], retrieved_titles)
    # Hand H1's run_attempt the retrieved chunks as the open-context.
    item = {
        **item,
        "paragraphs": retrieved,
        "context_sha256": _sha("\u0001".join(p["text"] for p in retrieved)),
    }
    record = await run_attempt(h, item, 1, DRAFT_PROMPT_CAPPED, RUN_TAG)
    record["slice"] = item.get("slice")
    record["retrieval"] = {
        "k": k,
        "topk_indices": topk_idx,
        "scores": scores,
        "retrieved_titles": retrieved_titles,
        "evidence_recall_at_10": recall,
        "embed_cost_usd": embed_cost,
        "embed_error": embed_err,
    }
    record["evidence_recall_at_10"] = recall
    record["n_retrieved"] = len(retrieved)
    return record, recall


def write_predictions(
    fh: Any, record: dict, run_tag: str, recall: float
) -> None:
    """One prediction row per arm (mirrors h1_run.main, +retrieval fields)."""
    for arm in ("off", "on"):
        a = (record.get("arms", {}) or {}).get(arm, {})
        fh.write(json.dumps({
            "run_tag": record.get("run_tag", run_tag),
            "item_id": record.get("item_id"),
            "dataset": record.get("dataset"),
            "slice": record.get("slice"),
            "repeat": record.get("repeat", 1),
            "shared_draft_hash": record.get("shared_draft_hash", ""),
            "arm": arm,
            "answer": a.get("final_answer", ""),
            "abstained": a.get("abstained", True),
            "citations": a.get("citations", []),
            "claims_grounded": a.get("claims_grounded", 0),
            "claims_total": a.get("claims_total", 0),
            "attempt_error": record.get(
                "attempt_error", record.get("draft_error", "")
            ),
            "evidence_recall_at_10": recall,
            "n_retrieved": record.get("n_retrieved", 0),
        }) + "\n")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="0 = all 150 frozen items")
    ap.add_argument("--dry-plan", action="store_true")
    ap.add_argument("--k", type=int, default=K, help="retrieval depth")
    ap.add_argument("--out-dir", default="", help="override run output directory")
    ap.add_argument("--run-tag", default="", help="override the ledger/run tag")
    args = ap.parse_args()

    items = load_frozen_items()
    attempts = items[: args.limit] if args.limit else list(items)
    run_tag = args.run_tag or RUN_TAG
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else RUNS
    arith_path = (out_dir / "pre_call_arithmetic.json") if args.out_dir else ARITH

    arithmetic = pre_call_arithmetic(len(attempts), run_tag=run_tag)
    arithmetic["retrieval_k"] = args.k
    arithmetic["slices"] = "hotpot:150"
    arith_path.parent.mkdir(parents=True, exist_ok=True)
    arith_path.write_text(json.dumps(arithmetic, indent=2), encoding="utf-8")
    print("=== PRE-CALL ARITHMETIC (H2) ===")
    print(json.dumps(arithmetic, indent=2))
    if args.dry_plan:
        print(f"\nplanned embed+draft attempts: {len(attempts)}")
        return 0
    if arithmetic.get("verdict") == "BUDGET-GATE":
        print(
            f"\n{arithmetic.get('verdict')}: {arithmetic.get('reason')}",
            file=sys.stderr,
        )
        return 3

    corpus = build_canonical_corpus()
    vectors = load_vectors()

    out_dir.mkdir(parents=True, exist_ok=True)
    h = H2Harness(run_tag=run_tag)
    traces_f = (out_dir / "traces.jsonl").open("w", encoding="utf-8")
    preds_f = (out_dir / "predictions.jsonl").open("w", encoding="utf-8")
    recalls: list[float] = []
    embeds: list[float] = []
    try:
        for n, item in enumerate(attempts, 1):
            try:
                record, recall = await run_attempt_h2(h, item, vectors, corpus, k=args.k)
            except Exception as exc:
                record = {
                    "run_tag": run_tag,
                    "item_id": item.get("item_id"),
                    "dataset": item.get("dataset"),
                    "question": item.get("question"),
                    "question_sha256": item.get("question_sha256"),
                    "context_sha256": "",
                    "n_paragraphs": 0,
                    "paragraphs": [],
                    "slice": item.get("slice"),
                    "attempt_error": f"{type(exc).__name__}: {exc}"[:200],
                    "arms": {},
                    "retrieval": {
                        "embed_cost_usd": 0.0,
                        "embed_error": f"{type(exc).__name__}: {exc}"[:200],
                    },
                    "evidence_recall_at_10": 0.0,
                    "n_retrieved": 0,
                }
                recall = 0.0
            traces_f.write(json.dumps(record) + "\n")
            write_predictions(preds_f, record, run_tag, recall)
            recalls.append(recall)
            embeds.append(float(record["retrieval"]["embed_cost_usd"]))
            if n % 20 == 0 or n == len(attempts):
                spent = sum(r["cost_usd"] for r in h.ledger_rows)
                last = recalls[-20:]
                mean_r = round(sum(last) / max(len(last), 1), 4)
                print(
                    f"[{n}/{len(attempts)}] spent=${spent:.6f} "
                    f"recall(last20)={mean_r}",
                    flush=True,
                )
    finally:
        traces_f.close()
        preds_f.close()
        write_ledger(h.ledger_rows)

    spent = round(sum(r["cost_usd"] for r in h.ledger_rows), 8)
    summary = {
        "run_tag": run_tag,
        "n_attempts": len(attempts),
        "k": args.k,
        "metered_calls": len(h.ledger_rows),
        "free_calls": sum(r["free_calls"] for r in h.ledger_rows),
        "paid_calls": sum(r["paid_calls"] for r in h.ledger_rows),
        "fallbacks": sum(r["fallbacks"] for r in h.ledger_rows),
        "failed_calls": sum(r["failed_calls"] for r in h.ledger_rows),
        "prompt_tokens": sum(r["prompt_tokens"] for r in h.ledger_rows),
        "completion_tokens": sum(r["completion_tokens"] for r in h.ledger_rows),
        "spent_usd": spent,
        "remaining_cap_usd": round(remaining_cap(), 6),
        "retrieval": {
            "mean_evidence_recall_at_10": round(
                sum(recalls) / max(len(recalls), 1), 6
            ),
            "n_items": len(recalls),
            "mean_embed_cost_usd": round(
                sum(embeds) / max(len(embeds), 1), 8
            ),
        },
    }
    (out_dir / "run_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
