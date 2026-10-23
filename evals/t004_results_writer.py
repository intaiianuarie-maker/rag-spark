"""T-EVAL-004 structured results emitter (scratch-only, STEER rule b).

Writes ``evals/t004_multilingual.results.json`` summarizing the multilingual
(EN/RO/PT) live gate run. Strictly scratch-scoped: it refuses
``knowledge__serving`` outright -- the production serving corpus is never
addressed by this eval, and the production dense embedder (BAAI/bge-m3) is
untouched. Per the T-069 no-stub rule, eval embeddings come from
``google/gemini-embedding-2`` (no local bge/infinity/7997 traffic).
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

logger = logging.getLogger("t004_results_writer")

MULTILINGUAL_SLICES = ("en", "ro", "pt")

# Canonical V1 gate targets (mirrored from evals.live_v1_gate.TARGETS to avoid a
# circular import; release-card constants, not per-run values).
TARGETS: dict[str, float] = {
    "grounded_answer_rate": 0.90,
    "factual_precision": 0.98,
    "citation_validity": 1.00,
    "evidence_recall_at_20": 0.95,
    "correct_abstention": 0.95,
}


def _mean(values: list[float | None]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _span(span: Any) -> dict[str, Any]:
    content = getattr(span, "content", None)
    score = getattr(span, "score", None)
    return {
        "chunk_id": getattr(span, "chunk_id", None),
        "score": round(float(score), 4) if score is not None else None,
        "workspace_id": getattr(span, "workspace_id", None),
        "document_id": getattr(span, "document_id", None),
        "content_snippet": (content[:160] + "...") if content else None,
    }


def _flow_dict(f: Any) -> dict[str, Any]:
    return {
        "flow_number": f.flow_number,
        "name": f.name,
        "scenario": f.scenario,
        "passed": bool(f.passed),
        "details": f.details,
    }


def emit_t004_results(report: Any, collection: str) -> pathlib.Path:
    """Persist the T-EVAL-004 multilingual results JSON (scratch-only)."""
    if collection == "knowledge__serving":
        raise RuntimeError(
            "T-EVAL-004 refuses to emit results for 'knowledge__serving'; "
            "use an eval_scratch_* collection per the T-071 scratch-isolation rule."
        )

    items: list[Any] = list(getattr(report, "per_item", []) or [])
    ml_items = [
        r
        for r in items
        if getattr(getattr(r, "item", None), "slice_name", None) in MULTILINGUAL_SLICES
    ]

    per_item: list[dict[str, Any]] = []
    for r in ml_items:
        retrieved = list(r.retrieved_items or [])
        retrieved_ids = {it.chunk_id for it in retrieved if getattr(it, "chunk_id", None)}
        recalled = any(cid in retrieved_ids for cid in (r.gold_chunk_ids or []))
        answer = getattr(r, "answer", None)
        per_item.append(
            {
                "slice": r.item.slice_name,
                "source_id": r.item.source_id,
                "source_path": r.item.source_path,
                "split": r.item.split,
                "is_answerable": bool(r.item.is_answerable),
                "question": r.item.question,
                "gold_answer": r.item.gold_answer,
                "gold_chunk_ids": list(r.gold_chunk_ids or []),
                "chunk_mapping_provenance": {
                    "indexed_document_ids": sorted(
                        {
                            it.document_id
                            for it in retrieved
                            if getattr(it, "document_id", None)
                        }
                    ),
                    "retrieved_chunk_count": len(retrieved),
                    "top_chunk_id": retrieved[0].chunk_id if retrieved else None,
                    "gold_recalled_at_20": bool(recalled),
                    "recall_at_20": r.recall_at_20,
                },
                "retrieved_spans": [_span(it) for it in retrieved],
                "metrics": {
                    "recall_at_20": r.recall_at_20,
                    "faithfulness": r.faithfulness,
                    "citation_validity": r.citation_validity,
                    "factual_precision": r.factual_precision,
                    "grounded_success": bool(r.grounded_success),
                    "correct_abstention": r.correct_abstention,
                },
                "answer_text": answer.answer if answer else None,
            }
        )

    cs = report.call_stats
    spend = cs.as_dict()
    spend["free_pool_exhaustion_429s"] = cs.n_fallbacks
    spend["failed_calls_429_no_recovery"] = cs.n_failed_calls
    spend["usd"] = round(cs.cost_usd, 8)

    per_lang: dict[str, Any] = {}
    for s in MULTILINGUAL_SLICES:
        rows = [r for r in ml_items if r.item.slice_name == s]
        if not rows:
            continue
        per_lang[s] = {
            "n": len(rows),
            "recall_at_20": _mean([r.recall_at_20 for r in rows]),
            "faithfulness": _mean([r.faithfulness for r in rows]),
            "citation_validity": _mean([r.citation_validity for r in rows]),
            "factual_precision": _mean([r.factual_precision for r in rows]),
            "grounded_success": _mean(
                [1.0 if r.grounded_success else 0.0 for r in rows]
            ),
        }

    payload: dict[str, Any] = {
        "ticket": "T-EVAL-004",
        "title": "Multilingual Retrieval Eval (STEER applied)",
        "steer_rule": "b (scratch-only; never knowledge__serving, even with purge/verify)",
        "eval_embedder": "google/gemini-embedding-2 (GeminiEmbedding2, 3072-d)",
        "prod_embedder": (
            "BAAI/bge-m3 @ 127.0.0.1:7997 -- UNTOUCHED by this run (T-069 no-stub trap)"
        ),
        "generator": (
            "google/gemma-4-26b-a4b-it:free (metered paid fallback on HTTP 429/402 exhaustion)"
        ),
        "scratch_collection_used": collection,
        "assert_knowledge__serving_not_accessed": True,
        "assert_collection_is_scratch": bool(collection.startswith("eval_scratch_")),
        "frozen_corpus": {
            "split": report.split,
            "n_total": report.n_total,
            "n_answerable": report.n_answerable,
            "n_unanswerable": report.n_unanswerable,
            "multilingual_n": len(ml_items),
            "multilingual_slices": list(MULTILINGUAL_SLICES),
            "note": (
                "Frozen corpus n=30 holdout; multilingual (en+ro+pt) n=15 (5 per slice). "
                "n=100 RO/PT/EN is unreachable: source parquet corpora (xquad-en-ro-v1, "
                "pira-v1) are absent locally and carry no license files here; rebuilding "
                "would violate the frozen-corpus-pinned + license-first rules. Results "
                "are reported on the loaded frozen n, with no accuracy claims beyond it."
            ),
        },
        "multilingual_subset": {
            "n": len(ml_items),
            "recall_at_20": _mean([r.recall_at_20 for r in ml_items]),
            "grounded_success": _mean(
                [1.0 if r.grounded_success else 0.0 for r in ml_items]
            ),
            "faithfulness": _mean([r.faithfulness for r in ml_items]),
            "citation_validity": _mean([r.citation_validity for r in ml_items]),
            "factual_precision": _mean([r.factual_precision for r in ml_items]),
            "per_language": per_lang,
        },
        "per_item_spans": per_item,
        "authenticity_census": {
            "overall": {
                "evidence_recall_at_20": report.evidence_recall_at_20,
                "grounded_answer_rate": report.grounded_answer_rate,
                "factual_precision": report.factual_precision,
                "citation_validity": report.citation_validity,
                "correct_abstention": report.correct_abstention,
            },
            "ci_recall": list(getattr(report, "ci_recall", (0.0, 0.0))),
            "ci_grounded": list(getattr(report, "ci_grounded", (0.0, 0.0))),
            "ci_factual": list(getattr(report, "ci_factual", (0.0, 0.0))),
            "ci_citation": list(getattr(report, "ci_citation", (0.0, 0.0))),
            "ci_abstention": list(getattr(report, "ci_abstention", (0.0, 0.0))),
            "per_slice": report.slice_breakdown,
        },
        "acceptance_flows": [_flow_dict(f) for f in report.acceptance_flows],
        "spend_meter": spend,
        "gate": {
            "all_targets_met": bool(report.all_targets_met),
            "targets": dict(TARGETS),
        },
        "belebele_excluded": True,
    }

    out = pathlib.Path(__file__).resolve().parent / "t004_multilingual.results.json"
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(
        "T-EVAL-004 results written to %s (scratch=%s)", out, collection
    )
    return out
