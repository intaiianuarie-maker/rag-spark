#!/usr/bin/env python
"""T-P01 step 1 — inventory every committed artifact for T-G01 replay traces.

T-G01 (6f03fac) is landed but flagged OFF. Plan §4.1 Experiment A wants to replay
*identical* upstream inputs — retrieved passages + generated candidate answer +
candidate citations + metadata — through Arm A (ExtractiveGrounder) vs Arm B
(ParaphraseTolerantGrounder, threshold 0.85) with generation and retrieval held
fixed, so the comparison isolates the grounder's decision logic from LLM variance.

This script answers one question, offline, with no model calls: **do such traces
exist in committed artifacts?** It does not generate, repair, or re-run anything.

A trace counts as REPLAYABLE only if a single record carries all three of:
  1. a candidate answer/claim text (something to re-ground),
  2. retrieved passage TEXT (something to ground it against — an id is not text),
  3. a citation / chunk reference (so citation resolution can be compared too).

Usage:  python evals/benchmark_v1/judges/trace_inventory.py [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

# evals/benchmark_v1/judges/<this file> -> repo root is parents[3]
_REPO = pathlib.Path(__file__).resolve().parents[3]

#: Fields that would evidence each required trace component.
ANSWER_KEYS = {
    "answer", "candidate_answer", "generated_answer", "on_answer", "off_answer",
    "response", "text", "claims", "statement", "final_output", "model_output",
}
PASSAGE_KEYS = {
    "content", "passage", "passages", "context", "contexts", "retrieved",
    "retrieved_text", "chunk_text", "evidence_text", "text", "body",
}
CITATION_KEYS = {
    "citations", "citation", "chunk_ids", "evidence_ids", "references",
    "chunk_id", "evidence_ids_cited", "cited_chunk_ids", "span",
}

#: Artifacts whose name marks them as not-grounding runs (tool-call batteries).
EXCLUDED_BY_NAME = ("bfcl", "gemma_call_probe", "graph_paired", "g5_paired")


def _walk(node: object, depth: int = 0) -> tuple[set[str], set[str], set[str]]:
    """Collect candidate key names appearing anywhere under ``node``."""
    answers: set[str] = set()
    passages: set[str] = set()
    citations: set[str] = set()
    if depth > 6:
        return answers, passages, citations
    if isinstance(node, dict):
        for key, value in node.items():
            k = str(key).lower()
            if k in ANSWER_KEYS:
                answers.add(k)
            if k in PASSAGE_KEYS:
                passages.add(k)
            if k in CITATION_KEYS:
                citations.add(k)
            a, p, c = _walk(value, depth + 1)
            answers |= a
            passages |= p
            citations |= c
    elif isinstance(node, list):
        for item in node[:50]:
            a, p, c = _walk(item, depth + 1)
            answers |= a
            passages |= p
            citations |= c
    return answers, passages, citations


def _record_count(data: object) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("records", "results", "items", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


def inventory() -> list[dict[str, object]]:
    """Report, per artifact, whether it can supply a replayable trace."""
    rows: list[dict[str, object]] = []
    evals = _REPO / "evals"
    for path in sorted(evals.rglob("*.json")):
        rel = path.relative_to(_REPO).as_posix()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # unreadable is itself a finding
            rows.append({"artifact": rel, "status": f"unreadable: {type(exc).__name__}"})
            continue
        answers, passages, citations = _walk(data)
        # A passage-bearing key that only ever held the answer is not a passage.
        replayable = bool(answers and passages and citations)
        rows.append(
            {
                "artifact": rel,
                "n_records": _record_count(data),
                "has_candidate_answer": sorted(answers),
                "has_passage_text": sorted(passages),
                "has_citation_refs": sorted(citations),
                "replayable": replayable,
                "excluded_by_name": any(tok in rel for tok in EXCLUDED_BY_NAME),
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", help="write the inventory as JSON")
    args = ap.parse_args()

    rows = inventory()
    replayable = [r for r in rows if r.get("replayable")]
    print(f"artifacts scanned: {len(rows)}")
    print(f"REPLAYABLE (answer + passage text + citations in one record set): {len(replayable)}")
    for r in replayable:
        print(f"  - {r['artifact']}")
    print()
    print("per-artifact detail:")
    for r in rows:
        if "status" in r:
            print(f"  {r['artifact']}: {r['status']}")
            continue
        print(
            f"  {r['artifact']}: n={r['n_records']} "
            f"answer={bool(r['has_candidate_answer'])} "
            f"passage={bool(r['has_passage_text'])} "
            f"cite={bool(r['has_citation_refs'])} "
            f"-> {'REPLAYABLE' if r['replayable'] else 'NOT replayable'}"
        )
        print(f"      answer_keys={r['has_candidate_answer']}")
        print(f"      passage_keys={r['has_passage_text']}")
        print(f"      citation_keys={r['has_citation_refs']}")

    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nwrote {out}")
    return 0 if replayable else 1


if __name__ == "__main__":
    sys.exit(main())