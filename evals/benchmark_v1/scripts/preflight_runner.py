"""T-P02 — preflight runner: bounded live first drafts, every call ledgered.

Dispatches at most N (<=20) development-only first drafts through the PRODUCTION
provider path (``OpenRouterProvider``), pricing each one and writing one ledger
row per draft. No judge, no scoring, no holdout ids.

Per-draft attribution uses a distinct ``call_site`` per question, so
``GLOBAL_ACCOUNTING`` yields exact token/cost/fallback/failure counts for that
draft instead of an aggregate we would have to apportion by guesswork.

Retrieval is deliberately NOT run here: the corpus for these ids is not on disk
(see the handoff row), so this prices the two metered legs the benchmark will
actually pay for — query embedding and first-draft generation.
"""

from __future__ import annotations

import csv
import datetime
import pathlib
import time
from typing import Any

from models.adapters.openrouter_provider import (
    DEFAULT_CHAT_FREE_MODEL,
    DEFAULT_CHAT_PAID_MODEL,
    GLOBAL_ACCOUNTING,
    OpenRouterProvider,
)
from models.interfaces import ChatMessage, GenerationRequest, MessageRole

EMBED_MODEL = "google/gemini-embedding-2"
FREE_MODEL = DEFAULT_CHAT_FREE_MODEL
PAID_MODEL = DEFAULT_CHAT_PAID_MODEL

PROMPT = (
    "Answer the question using ONLY the supplied context. "
    "If the context does not contain the answer, say so.\n\n"
    "Context:\n{ctx}\n\nQuestion: {q}"
)


def _snapshot() -> dict[str, float]:
    t = GLOBAL_ACCOUNTING.summary()["total"]
    return {
        "calls": float(t.get("calls", 0)),
        "free": float(t.get("free_calls", 0)),
        "paid": float(t.get("paid_calls", 0)),
        "fallbacks": float(t.get("fallbacks", 0)),
        "failures": float(t.get("failed_calls", 0)),
        "prompt_tokens": float(t.get("prompt_tokens", 0)),
        "completion_tokens": float(t.get("completion_tokens", 0)),
        "vectors": float(t.get("vectors", 0)),
        "cost_usd": float(t.get("cost_usd", 0.0)),
    }


def _delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {k: round(after[k] - before[k], 8) for k in before}


def _rows_for_call_site(site: str) -> dict[str, Any]:
    stats = GLOBAL_ACCOUNTING.get_stats(site)
    return stats.as_dict()


LEDGER_FIELDS = [
    "timestamp_utc",
    "question_id",
    "item_type",
    "n_docs",
    "provenance",
    "embed_model",
    "gen_model_requested",
    "gen_model_used",
    "embed_calls",
    "embed_prompt_tokens",
    "gen_calls",
    "gen_fallbacks",
    "gen_failures",
    "prompt_tokens",
    "completion_tokens",
    "embedding_cost_usd",
    "generation_cost_usd",
    "total_cost_usd",
    "latency_ms",
    "error",
]


async def _one_draft(
    provider: OpenRouterProvider,
    item: dict[str, Any],
    docs_dir: pathlib.Path,
) -> dict[str, Any]:
    qid = str(item["id"])
    site = f"preflight_{qid}"
    ctx_parts: list[str] = []
    for doc_id in item.get("all_doc_ids", [])[:10]:
        p = docs_dir / f"{doc_id}.txt"
        if p.exists():
            ctx_parts.append(p.read_text(encoding="utf-8").strip())
    context = "\n\n".join(ctx_parts)[:12000]
    prompt = PROMPT.format(ctx=context, q=item["question"])

    before = _snapshot()
    t0 = time.monotonic()
    error = ""
    gen_used = ""
    try:
        # Leg 1 — query embedding (production provider, production model).
        await provider.embed_query(item["question"])
        # Leg 2 — first draft, production free-first generation with paid fallback.
        result = await provider.generate(
            GenerationRequest(
                model=FREE_MODEL,
                messages=[ChatMessage(role=MessageRole.USER, content=prompt)],
            ),
            call_site=site,
        )
        gen_used = result.model or ""
    except Exception as exc:  # provider outage / quota — recorded, never hidden
        error = f"{type(exc).__name__}: {exc}"[:300]
    latency_ms = int((time.monotonic() - t0) * 1000)

    d = _delta(before, _snapshot())
    site_stats = _rows_for_call_site(site)
    embed_cost = 0.0
    # Attribute embedding cost as (total delta cost) - (generation site cost).
    gen_cost = float(site_stats.get("cost_usd", 0.0))
    embed_cost = round(max(d["cost_usd"] - gen_cost, 0.0), 10)
    total = round(d["cost_usd"], 10)
    return {
        "timestamp_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "question_id": qid,
        "item_type": item.get("type", ""),
        "n_docs": len(item.get("all_doc_ids", [])),
        "provenance": item.get("provenance", ""),
        "embed_model": EMBED_MODEL,
        "gen_model_requested": FREE_MODEL,
        "gen_model_used": gen_used or (PAID_MODEL if d["fallbacks"] else ""),
        "embed_calls": int(d["calls"]) - int(site_stats.get("calls", 0)),
        "embed_prompt_tokens": 0,
        "gen_calls": int(site_stats.get("calls", 0)),
        "gen_fallbacks": int(site_stats.get("fallbacks", 0)),
        "gen_failures": int(site_stats.get("failed_calls", 0)),
        "prompt_tokens": int(d["prompt_tokens"]),
        "completion_tokens": int(d["completion_tokens"]),
        "embedding_cost_usd": embed_cost,
        "generation_cost_usd": gen_cost,
        "total_cost_usd": total,
        "latency_ms": latency_ms,
        "error": error,
    }


async def run_preflight(
    ids: list[dict[str, Any]],
    *,
    ledger_path: pathlib.Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    provider = OpenRouterProvider(default_call_site="preflight")
    docs_dir = _REPO_DOCS()
    rows: list[dict[str, Any]] = []
    for item in ids:
        rows.append(await _one_draft(provider, item, docs_dir))

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LEDGER_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    spent = round(sum(r["total_cost_usd"] for r in rows), 10)
    per_draft = [r["total_cost_usd"] for r in rows if not r["error"]]
    mean = (sum(per_draft) / len(per_draft)) if per_draft else 0.0
    worst = max(per_draft) if per_draft else 0.0
    fallbacks = sum(r["gen_fallbacks"] for r in rows)
    errors = sum(1 for r in rows if r["error"])

    cap = 2.00
    reserve = 0.20
    shared = mean * 373 * (1 + reserve)
    unshared = mean * 746 * (1 + reserve)
    remaining = cap - spent
    fits = max(shared, unshared) <= remaining
    summary = {
        "n_drafts": len(rows),
        "n_errors": errors,
        "n_fallbacks": fallbacks,
        "free_tier_served": sum(1 for r in rows if r["gen_fallbacks"] == 0 and not r["error"]),
        "spent_usd": spent,
        "mean_cost_per_draft_usd": round(mean, 8),
        "worst_cost_per_draft_usd": round(worst, 8),
        "cap_usd": cap,
        "remaining_usd": round(remaining, 6),
        "reserve_fraction": reserve,
        "projection_shared_373": round(shared, 4),
        "projection_unshared_746": round(unshared, 4),
        "worst_case_projection_unshared": round(worst * 746 * (1 + reserve), 4),
        "verdict": "GO" if fits else "NOT-EXECUTED",
    }
    return rows, summary


def _REPO_DOCS() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3] / "evals" / "ingest" / "docs"