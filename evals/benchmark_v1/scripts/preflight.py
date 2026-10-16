#!/usr/bin/env python
"""T-P02 — Phase 0 metered preflight (benchmark plan §10.2).

Runs a bounded set of LIVE FIRST DRAFTS on DEVELOPMENT-ONLY ids to price the
full design before the confirmatory run. Hard ceiling: **$2.00 billed-service**
including this preflight (PREREGISTRATION.md).

Safety order is enforced by this script, not by convention:

1. The rate card is loaded from a **pinned snapshot** (``rate_card_snapshot.json``)
   captured from OpenRouter's free ``/models`` endpoint with its timestamp.
2. **The worst-case arithmetic is computed and gated BEFORE the first metered
   call.** If even the worst case breaches the remaining cap, the script exits
   ``BUDGET-GATE`` having spent nothing.
3. Only then are drafts dispatched, one at a time, each ledgered.
4. A projection for the full design (shared 373 / unshared 746 drafts + repairs
   + 20% reserve) decides ``GO`` / ``NOT-EXECUTED``.

No judge, no scoring, no holdout ids: this prices the pipeline only.

Usage:
    python evals/benchmark_v1/scripts/preflight.py --max-drafts 16
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parents[3]
for _p in (_REPO / "packages", _REPO / "apps" / "api", _REPO / "apps" / "worker"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

REPORTS = _REPO / "evals" / "benchmark_v1" / "reports"
LEDGER = REPORTS / "budget_ledger.csv"
RATE_CARD = REPORTS / "rate_card_snapshot.json"

#: Preregistered hard ceiling for ALL billed service in Phase 0, preflight included.
BUDGET_CAP_USD = 2.00
#: Reserve the plan requires on top of the point estimate.
RESERVE_FRACTION = 0.20

#: Production generation model + its paid fallback (packages/agents/policy.py).
GEN_FREE = "google/gemma-4-26b-a4b-it:free"
GEN_PAID = "google/gemma-4-26b-a4b-it"
#: Production embedding model (core.config / OpenRouterProvider).
EMBED_MODEL = "google/gemini-embedding-2"

#: Worst-case per-draft shape used for the PRE-CALL gate. Deliberately
#: pessimistic: it assumes the free tier is unavailable and every attempt needs
#: the maximum bounded retries the runtime may take.
ASSUMED_PROMPT_TOKENS = 4000
ASSUMED_COMPLETION_TOKENS = 512
ASSUMED_EMBED_CALLS = 5
ASSUMED_MAX_ATTEMPTS = 3

#: Empirical embedding price, derived from the T-EVAL-RUN ledger (the only
#: auditable source: the embedding model is not in OpenRouter's public
#: catalogue, so there is no published rate card to pin for it).
EMBED_USD_PER_CALL_UPPER_BOUND = 0.0000239

#: Full-design draft counts from the plan.
SHARED_DRAFTS = 373
UNSHARED_DRAFTS = 746


class BudgetGate(RuntimeError):
    """The pre-call worst case breaches the remaining cap — spend nothing."""


def load_rate_card() -> dict[str, float]:
    """Generation $/1M tokens from the pinned snapshot."""
    data = json.loads(RATE_CARD.read_text(encoding="utf-8"))
    paid = data["models"][GEN_PAID]["pricing"]
    return {
        "prompt_per_1m": float(paid["prompt"]) * 1_000_000,
        "completion_per_1m": float(paid["completion"]) * 1_000_000,
        "retrieved_utc": data["retrieved_utc"],
    }


def generation_cost(card: dict[str, float], prompt: int, completion: int) -> float:
    return (
        prompt / 1_000_000 * card["prompt_per_1m"]
        + completion / 1_000_000 * card["completion_per_1m"]
    )


def worst_case_per_draft(card: dict[str, float]) -> dict[str, float]:
    """Pessimistic cost of ONE draft including retries and embeddings."""
    one = generation_cost(
        card, ASSUMED_PROMPT_TOKENS, ASSUMED_COMPLETION_TOKENS
    ) + ASSUMED_EMBED_CALLS * EMBED_USD_PER_CALL_UPPER_BOUND
    return {"one_attempt_usd": one, "with_retries_usd": one * ASSUMED_MAX_ATTEMPTS}


def pre_call_arithmetic(card: dict[str, float], max_drafts: int) -> dict[str, object]:
    """Compute and GATE the worst case BEFORE any metered call happens."""
    wc = worst_case_per_draft(card)
    preflight_worst = wc["with_retries_usd"] * max_drafts
    remaining_before = BUDGET_CAP_USD

    shared = wc["with_retries_usd"] * SHARED_DRAFTS
    unshared = wc["with_retries_usd"] * UNSHARED_DRAFTS
    out = {
        "rate_card_retrieved_utc": card["retrieved_utc"],
        "generation_model_paid": GEN_PAID,
        "generation_model_free": GEN_FREE,
        "embed_model": EMBED_MODEL,
        "prompt_usd_per_1m": card["prompt_per_1m"],
        "completion_usd_per_1m": card["completion_per_1m"],
        "assumed_prompt_tokens": ASSUMED_PROMPT_TOKENS,
        "assumed_completion_tokens": ASSUMED_COMPLETION_TOKENS,
        "assumed_embed_calls": ASSUMED_EMBED_CALLS,
        "assumed_max_attempts": ASSUMED_MAX_ATTEMPTS,
        "worst_case_one_attempt_usd": round(wc["one_attempt_usd"], 8),
        "worst_case_per_draft_usd": round(wc["with_retries_usd"], 8),
        "preflight_worst_case_usd": round(preflight_worst, 6),
        "remaining_cap_before_usd": remaining_before,
        "full_design": {
            "shared_373_usd": round(shared, 4),
            "shared_373_plus_reserve_usd": round(shared * (1 + RESERVE_FRACTION), 4),
            "unshared_746_usd": round(unshared, 4),
            "unshared_746_plus_reserve_usd": round(unshared * (1 + RESERVE_FRACTION), 4),
        },
    }
    # THE GATE: refuse to spend if preflight alone could breach the cap.
    if preflight_worst >= remaining_before:
        out["verdict"] = "BUDGET-GATE"
        raise BudgetGate(
            f"preflight worst case ${preflight_worst:.4f} >= remaining cap "
            f"${remaining_before:.2f}; stopping before any metered call"
        )
    out["verdict"] = "PROCEED"
    return out


def select_dev_ids(n: int) -> list[dict[str, str]]:
    """DEV-ONLY ids: 2wiki validation items, proven disjoint from the frozen 150.

    The frozen sample is HotpotQA; these are 2wikimultihopqa validation items, so
    the intersection is empty by dataset (asserted below, not assumed).
    """
    frozen = json.loads(
        (_REPO / "evals" / "benchmark_v1" / "datasets" / "frozen_sample_150.json").read_text()
    )
    frozen_ids = set(frozen["bridge"]) | set(frozen["comparison"])
    manifest = json.loads((_REPO / "evals" / "ingest" / "manifest.json").read_text())
    items = manifest["items"]

    overlap = {i["id"] for i in items} & frozen_ids
    assert not overlap, f"DEV/holdout contamination: {sorted(overlap)[:5]}"

    # Cover every reasoning type (bridge/comparison/inference/compositional) and
    # take the longest-context items, so the preflight exercises the heavy shape.
    by_type: dict[str, list[dict]] = {}
    for it in items:
        by_type.setdefault(it["type"], []).append(it)
    picked: list[dict] = []
    seen: set[str] = set()
    # 2 per type first so bridge/comparison/inference/compositional are all
    # represented, then fill to n with the longest remaining contexts.
    def _take(pool: list[dict], k: int) -> None:
        for it in pool:
            if len(picked) >= n:
                return
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            picked.append(it)
            if sum(1 for p in picked if p["type"] == it["type"]) >= k:
                return

    for t in sorted(by_type):
        _take(sorted(by_type[t], key=lambda x: -len(x["all_doc_ids"])), 2)
    rest = sorted(
        (i for i in items if i["id"] not in seen),
        key=lambda x: -len(x["all_doc_ids"]),
    )
    _take(rest, n)
    picked = picked[:n]
    for p in picked:
        p["provenance"] = "neutral"  # produced before any T-G01 arm existed
    return picked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-drafts", type=int, default=16,
                    help="hard ceiling on metered first drafts (must be <= 20)")
    ap.add_argument("--plan-only", action="store_true",
                    help="print the pre-call arithmetic and exit without spending")
    args = ap.parse_args()

    if args.max_drafts > 20:
        print("REFUSED: --max-drafts above the 20-draft ceiling", file=sys.stderr)
        return 2

    card = load_rate_card()
    try:
        arithmetic = pre_call_arithmetic(card, args.max_drafts)
    except BudgetGate as exc:
        print(json.dumps({"verdict": "BUDGET-GATE", "reason": str(exc)}, indent=2))
        return 3

    print("=== PRE-CALL ARITHMETIC (before any metered call) ===")
    print(json.dumps(arithmetic, indent=2))
    (REPORTS / "preflight_arithmetic.json").write_text(
        json.dumps(arithmetic, indent=2), encoding="utf-8"
    )
    if args.plan_only:
        return 0

    ids = select_dev_ids(args.max_drafts)
    (REPORTS / "preflight_dev_ids.json").write_text(
        json.dumps({"ids": [i["id"] for i in ids], "n": len(ids),
                    "dataset": "2wikimultihopqa validation (disjoint from frozen HotpotQA 150)"},
                   indent=2),
        encoding="utf-8",
    )
    print(f"\n=== DEV-ONLY ids selected: {len(ids)} (recorded) ===")
    for i in ids:
        print(f"  {i['id']}  type={i['type']}  docs={len(i['all_doc_ids'])}")

    from preflight_runner import run_preflight

    rows, summary = asyncio.run(run_preflight(ids, ledger_path=LEDGER))
    print("\n=== LEDGER ===")
    for r in rows:
        print(f"  {r['question_id']}  used={r['gen_model_used'] or '-'}  "
              f"gen={r['generation_cost_usd']}  embed={r['embedding_cost_usd']}  "
              f"total={r['total_cost_usd']}  fallbacks={r['gen_fallbacks']}  "
              f"err={r['error'][:40]}")
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    (REPORTS / "preflight_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return 0 if summary["verdict"] == "GO" else 1


__main__ = main
if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    raise SystemExit(main())