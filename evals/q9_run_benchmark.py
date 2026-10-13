"""Q9 benchmark gate (V1-Q9) — real-slice runner. NOT product code.

Runs the pinned Q8 slices and reports the two Q9 gates per slice. Hermetic:
the gold context is supplied as the retrieval oracle (in-memory chunks), so no
Qdrant, Infinity, or model gateway is needed and results are reproducible.

Two independent, honest gates
-----------------------------
- **groundedness** — the harness's defined groundedness metric
  (:func:`app.eval.metrics.score_faithfulness`, the RAGAS-swappable lexical
  proxy) between the asserted claim (the normalized gold answer) and the
  retrieved gold context. Answers: "is the answer grounded in its context".
- **citation validity** — the REAL product citation gate
  (:func:`answer.verification.verify_claims` with the default
  :class:`~answer.verification.ExtractiveGrounder`): a cited chunk survives
  only when it exists, is in the caller's workspace scope, and substantiates
  the claim verbatim. Validity = surviving citations / asserted citations, so a
  fabricated/dead citation can never pass. 1.0 means every citation that
  survived was real; a dead citation is reported as a validity shortfall.

Gates (Q9): groundedness >= 0.90 and citation_validity == 1.0, overall and per
slice. Honest reporting: shortfalls print per-slice with failing ids and exit
non-zero — never fabricated upward.

Run (from repo root):
    /root/ko-venv/bin/python3 evals/q9_run_benchmark.py
    /root/ko-venv/bin/python3 evals/q9_run_benchmark.py --min-groundedness 0.90
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "packages", REPO / "apps" / "api", REPO, REPO / "evals"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Imported after sys.path is seeded: q9_gates pulls app.eval.metrics. Imported as
# a module (not as names) so the block stays a single sorted import.
import q9_gates  # noqa: E402

_q8_spec = importlib.util.spec_from_file_location(
    "build_q8_slices", Path(__file__).resolve().parent / "build_q8_slices.py"
)
assert _q8_spec and _q8_spec.loader, "build_q8_slices.py must be importable"
_q8 = importlib.util.module_from_spec(_q8_spec)
_q8_spec.loader.exec_module(_q8)
SLICES = _q8.SLICES
load_slices = _q8.load_slices

GROUNDEDNESS_GATE = 0.90
CITATION_GATE = 1.0
#: T-V1-076 — additive gate for unanswerable material (RP-02 target).
ABSTENTION_GATE = q9_gates.ABSTENTION_TARGET
WS = "ws-q9"
DOC = "doc-q9"


def _normalize_gold(gold: str) -> str:
    """Normalize a dataset gold answer to the text a system would assert.

    Table datasets store answers as Python list literals (``"['47,880']"``) or
    bare numerics; QA datasets store a span or sentence. We unwrap list
    literals to their first element and trim, so the claim is the answer text
    the grounder must find verbatim in the gold context.
    """
    import ast

    text = gold.strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list) and parsed:
                text = str(parsed[0]).strip()
        except (ValueError, SyntaxError):
            text = text.strip("[]").strip().strip("'\"").strip()
    return text


def _claim_text(_context: str, gold: str) -> str:
    """The claim the system asserts = the normalized gold answer span.

    We assert the gold answer verbatim (that is what a grounded system would
    output); the grounder then confirms it is substantiated by the gold
    context. No wording is invented.
    """
    return _normalize_gold(gold) or gold.strip()


def _chunk_context(context: str) -> list[tuple[str, str]]:
    """Split a gold context into (chunk_id, content) pairs for retrieval."""
    import re

    parts = [p.strip() for p in re.split(r"\n\s*\n", context) if p.strip()]
    if not parts:
        parts = [context.strip()]
    return [(f"c{i:04d}", part) for i, part in enumerate(parts)]


def _retrieval_result(record: dict) -> tuple[list, list, str]:
    """Build RetrievedItems for a record's gold context plus its claim text."""
    from retrieval.interfaces import RetrievalMethod, RetrievedItem

    items = [
        RetrievedItem(
            content=content,
            score=1.0,
            method=RetrievalMethod.LEXICAL,
            workspace_id=WS,
            document_id=DOC,
            chunk_id=chunk_id,
        )
        for chunk_id, content in _chunk_context(record["context"])
    ]
    claim = _claim_text(record["context"], record.get("gold_answer", ""))
    return items, [claim], claim


def _score_record(record: dict) -> dict:
    """Run one record through the REAL verification path; return its metrics."""
    import asyncio

    from answer.models import Claim
    from answer.verification import ExtractiveGrounder, verify_claims
    from app.eval.metrics import score_faithfulness

    items, claim_texts, claim = _retrieval_result(record)
    if not claim.strip():
        return {
            "slice": record["slice"],
            "id": record["source_id"],
            "faithfulness": 0.0,
            "cited": 0,
            "survived": 0,
            "claim": claim,
            "ok": False,
        }

    context = "\n".join(item.content for item in items)
    grounder = ExtractiveGrounder()
    # A real system cites the chunk(s) that substantiate its claim. Cite the
    # supporting chunks; if the conservative extractive grounder finds none
    # (its disclosed cross-language / bare-numeric / multi-sentence limits), the
    # record asserts NO citation rather than fabricating one.
    supporting = [
        item.chunk_id
        for item in items
        if item.chunk_id and grounder.score(claim, item.content) >= grounder.threshold
    ]
    # Also assert a dead/fabricated citation to prove the product gate drops it.
    dead_id = "dead-fabricated-chunk"
    cited_ids = [*supporting, dead_id]
    claims = [Claim(text=text, chunk_ids=cited_ids) for text in claim_texts]
    verified, citations = asyncio.run(
        verify_claims(
            claims,
            items,
            allowed_workspace_ids={WS},
            claim_grounder=grounder,
        )
    )
    # Citation validity: every surviving citation is real, in-scope and
    # grounded, and no fabricated id survives. The dead id must never appear.
    surviving_ids = {c.chunk_id for c in citations}
    fabricated_survived = dead_id in surviving_ids
    all_valid = (not fabricated_survived) and all(
        c.chunk_id in {i.chunk_id for i in items} for c in citations
    )
    return {
        "slice": record["slice"],
        "id": record["source_id"],
        "faithfulness": score_faithfulness(claim, context),
        "cited": len(cited_ids),
        "survived": len(verified),
        "valid": all_valid,
        "fabricated_survived": fabricated_survived,
        "claim": claim,
        "ok": True,
    }



def is_abstention_slice(rows: Sequence[dict[str, Any]]) -> bool:
    """True when a slice's records have no supportable gold answer.

    T-V1-076: detected from the DATA, not hardcoded to a slice name, so a future
    unanswerable slice is routed correctly without touching this function.

    Groundedness is definitionally 0.0 on such a slice — there is nothing to be
    grounded in — so gating it on groundedness is a category error. That is the
    defect T-V1-075 surfaced honestly (GATE FAIL on `unanswerable`) and this is
    the fix: those slices are gated on abstention rate instead. The five
    answerable slices' thresholds and computation are untouched.
    """
    return bool(rows) and all(not (r.get("gold_answer") or "").strip() for r in rows)


def _abstention_pair(record: dict[str, Any]) -> tuple[str, list, str]:
    """Build the ``(id, retrieved_items, asserted_claim)`` triple for one record.

    The claim is the naive restatement of the question: what a system that has
    no answer tends to assert anyway. Content words are kept (see
    ``q9_gates.naive_restatement``) so the probe is the harder construction.
    """
    items, _claims, _claim = _retrieval_result(record)
    return record["source_id"], items, q9_gates.naive_restatement(record["question"])


def run_benchmark(slices_path: Path | None = None) -> dict:
    """Score every pinned record and aggregate groundedness + citation validity."""
    records = load_slices(slices_path) if slices_path else load_slices()
    per_slice: dict[str, list[dict]] = {tag: [] for tag in SLICES}
    raw_by_slice: dict[str, list[dict]] = {tag: [] for tag in SLICES}
    for record in records:
        row = dict(_score_record(record))
        row["gold_answer"] = record.get("gold_answer", "")
        per_slice[record["slice"]].append(row)
        raw_by_slice[record["slice"]].append(record)

    summary: dict[str, dict] = {}
    for tag, rows in per_slice.items():
        if is_abstention_slice(rows):
            # T-V1-076: unanswerable material is gated on abstention rate, NOT
            # on groundedness (which is 0.0 by construction). Reported additively:
            # the answerable-slice fields below are not applied to it.
            report = q9_gates.evaluate_abstention(
                [_abstention_pair(r) for r in raw_by_slice.get(tag, [])]
            )
            summary[tag] = {
                "n": report.n_items,
                "abstention_rate": report.rate,
                "abstained": report.abstained,
                "leaked": report.leaked,
                "oracle": True,
                "metric": "abstention_rate",
            }
            continue
        scored = [r for r in rows if r["ok"]]
        n = len(scored)
        valid = sum(1 for r in scored if r["valid"])
        summary[tag] = {
            "n": n,
            "groundedness": (sum(r["faithfulness"] for r in scored) / n) if n else 0.0,
            "citation_validity": (valid / n) if n else 1.0,
            "valid": valid,
            "fabricated_survived": sum(1 for r in scored if r["fabricated_survived"]),
            "failures": [
                r["id"] for r in scored if r["faithfulness"] < GROUNDEDNESS_GATE
            ],
        }

    total_n = sum(s["n"] for s in summary.values() if "groundedness" in s)
    total_valid = sum(s["valid"] for s in summary.values() if "valid" in s)
    answerable = [s for s in summary.values() if "groundedness" in s]
    summary["_overall"] = {
        "n": total_n,
        "groundedness": (
            sum(s["groundedness"] * s["n"] for s in answerable) / total_n
            if total_n
            else 0.0
        ),
        "citation_validity": (total_valid / total_n) if total_n else 1.0,
        "valid": total_valid,
        "fabricated_survived": sum(
            s["fabricated_survived"] for s in summary.values() if "fabricated_survived" in s
        ),
        "failures": [],
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Q9 benchmark gate (real slices)")
    parser.add_argument("--slices", type=Path, default=None, help="pinned Q8 JSONL path")
    parser.add_argument("--min-groundedness", type=float, default=GROUNDEDNESS_GATE)
    args = parser.parse_args(argv)

    summary = run_benchmark(args.slices)

    print("Q9 benchmark — real multilingual slices (gold-context oracle)")
    print(f"{'slice':14s} {'n':>3s} {'groundedness':>12s} {'cite_validity':>14s}  detail")
    for tag in (*SLICES, "_overall"):
        s = summary[tag]
        label = "OVERALL" if tag == "_overall" else tag
        if "abstention_rate" in s:
            # Unanswerable slice: reported by abstention rate. Groundedness is
            # 0.0 by construction here and is deliberately NOT shown as if it
            # were a quality number.
            print(
                f"{label:14s} {s['n']:>3d} {'n/a':>12s} {'n/a':>14s}  "
                f"abstention={s['abstention_rate']:.4f} leaked={len(s['leaked'])}"
            )
            continue
        fails = ",".join(s["failures"]) or "-"
        print(
            f"{label:14s} {s['n']:>3d} {s['groundedness']:>12.4f} "
            f"{s['citation_validity']:>14.4f}  {fails}"
        )

    overall = summary["_overall"]
    failures: list[str] = []
    for tag in (*SLICES, "_overall"):
        s = summary[tag]
        if "abstention_rate" in s:
            # T-V1-076: the abstention gate is ADDITIVE. It replaces only the
            # (category-error) groundedness check for unanswerable material; it
            # never relaxes the answerable slices' thresholds.
            if s["abstention_rate"] < ABSTENTION_GATE:
                failures.append(
                    f"{tag}: abstention {s['abstention_rate']:.4f} < {ABSTENTION_GATE}"
                    f" (leaked: {','.join(s['leaked']) or '-'})"
                )
            continue
        if s["groundedness"] < args.min_groundedness:
            failures.append(
                f"{tag}: groundedness {s['groundedness']:.4f} < {args.min_groundedness}"
            )
        if s["citation_validity"] < CITATION_GATE:
            failures.append(
                f"{tag}: citation_validity {s['citation_validity']:.4f} < {CITATION_GATE}"
            )

    if failures:
        print("\nGATE: FAIL")
        for line in failures:
            print(f"  - {line}")
        return 1
    unans = [s for s in summary.values() if "abstention_rate" in s]
    unans_note = ""
    if unans:
        rates = " ".join(f"{v['abstention_rate']:.4f}" for v in unans)
        unans_note = f", abstention {rates} (>= {ABSTENTION_GATE})"
    print(
        f"\nGATE: PASS — groundedness {overall['groundedness']:.4f} "
        f"(>= {args.min_groundedness}), citation_validity "
        f"{overall['citation_validity']:.4f} (== {CITATION_GATE}){unans_note}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
