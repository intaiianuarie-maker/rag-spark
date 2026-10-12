"""G5 paired graph-vs-baseline eval driver (V2 work package G5). NOT product code.

Runs the paired, equal-K graph-vs-baseline evaluation from
``tests/fixtures/graph_paired_eval.py`` and prints a comparison report at two
context windows so the orchestrator's default truncation of the graph leg is
visible. Zero external services, zero model weights, zero keys (deterministic
fakes).

Honesty contract
----------------
Every report line discloses TWO independent tags:

- **dataset origin**: ``synthetic`` (shipped mechanism corpus) or ``labelled``
  (a real labelled dataset via ``--dataset``).
- **runtime mode**: ``fake`` (one-hot fake embeddings, in-memory fake stores,
  token-overlap fake reranker) -- ALWAYS, for any input. Real labels do NOT make
  this the V1 production retrieval conditions; a labelled dataset on this
  runtime is still mechanism-only.

**grounded-success is UNMEASURED** here: no answer engine/provider is invoked
and no produced answer/claim/citation is inspected. The harness reports **oracle
gold-answer availability** (is the provided gold answer verbatim in a retrieved
chunk) as a retrieval diagnostic -- explicitly NOT answer success.

If ``--dataset PATH`` is given it must load (single workspace, resolvable chunk
refs, no conflicting duplicates); otherwise the driver refuses to run and reports
the missing/incompatible dataset honestly rather than fabricating numbers.

Run (from repo root):
    /root/ko-venv/bin/python3 evals/graph_paired_eval.py
    /root/ko-venv/bin/python3 evals/graph_paired_eval.py --dataset path/to/labels.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# The pyproject ``pythonpath`` only applies under pytest; add it here so the
# driver runs standalone. Same paths Makefile/compose use.
_REPO = Path(__file__).resolve().parents[1]
_evals_dir = str(Path(__file__).resolve().parent)
if _evals_dir in sys.path:
    sys.path.remove(_evals_dir)
for _p in (_REPO / "packages", _REPO / "apps" / "api", _REPO, _REPO / "tests" / "fixtures"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from graph_paired_eval import (  # noqa: E402 - must follow sys.path insert above
    DEFAULT_K,
    DatasetValidationError,
    load_labelled_dataset,
    run_paired_eval,
    synthetic_multihop_dataset,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="G5 paired graph-vs-baseline eval")
    parser.add_argument(
        "--dataset",
        default=None,
        help="Path to a real labelled multi-hop JSONL dataset (optional).",
    )
    parser.add_argument("--k", type=int, default=DEFAULT_K, help="Retrieval budget K.")
    parser.add_argument(
        "--distractors",
        type=int,
        default=8,
        help="Number of keyword near-miss distractors in the synthetic corpus.",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    dataset = None
    if args.dataset:
        try:
            dataset = load_labelled_dataset(args.dataset)
        except DatasetValidationError as exc:
            print(f"ERROR: incompatible labelled dataset: {args.dataset}")
            print(f"  {exc}")
            print("No evaluation performed. Refusing to fabricate numbers.")
            return 2
        if dataset is None:
            print(f"ERROR: labelled dataset not found or empty: {args.dataset}")
            print("No evaluation performed. Reporting the missing dataset honestly;")
            print("refusing to fabricate numbers from a different corpus.")
            return 2
    else:
        dataset = synthetic_multihop_dataset(n_distractors=args.distractors)
        print("NOTE: no --dataset supplied; using the SYNTHETIC mechanism corpus.")
        print("Runtime is FAKE (fake embeddings/stores/reranker): mechanism-only.")
        print("grounded-success is UNMEASURED (no answer engine invoked).")
        print("Real Q8 multi-hop labels are missing.")
        print()

    # Report at the orchestrator default context window and a wide window so the
    # truncation/dilution of the graph leg is visible rather than hidden.
    for ctx in (6, args.k):
        report = await run_paired_eval(dataset, k=args.k, context_top_k=ctx)
        for line in report.summary_lines():
            print(line)
        for ex in report.per_example:
            print(
                f"    [{ex.example_id}] recall base={ex.baseline.recall_at_k:.3f} "
                f"graph={ex.graph.recall_at_k:.3f} (delta {ex.recall_delta:+.3f}) | "
                f"oracle base={ex.baseline.oracle_available:.1f} "
                f"graph={ex.graph.oracle_available:.1f}"
            )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
