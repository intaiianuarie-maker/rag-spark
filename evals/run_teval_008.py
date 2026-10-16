"""T-EVAL-008 runner — executes the embedder separation protocol.

Usage:  python3 -m evals.run_teval_008 [n_hotpot]

Runs both legs (gemini-embedding-2 via OpenRouter cloud + bge-m3 via OpenRouter
as the T-EVAL-008-scoped paid substitute for the removed local infinity service),
prints the number tables, and writes the JSON report to evals/t-eval-008-results.json.

Cost: ~$0.001 total for the default run (ticket-scoped paid cloud auth).
"""
from __future__ import annotations

import json
import sys

from evals.embedder_clients import BgeM3Local, GeminiEmbedding2
from evals.embedder_separation import build_report

OUTPUT_PATH = "evals/t-eval-008-results.json"


def main() -> None:
    n_hotpot = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    embedders = [GeminiEmbedding2(), BgeM3Local()]
    report = build_report(embedders, n_hotpot=n_hotpot)
    for e in embedders:
        e.close()

    # Write JSON report
    with open(OUTPUT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # Print human-readable summary
    print("=== T-EVAL-008 FINAL REPORT ===")
    print()

    print("--- EMBEDDERS ---")
    for e in report["embedders"]:
        print(f'  {e["name"]:30s} dim={e["dimension"]} priced={e["priced"]}')
    print()

    print("--- SEPARATION (mean cosine; identical texts, identical metric code) ---")
    print(f'  {"embedder":30s} {"pair_set":30s} {"n":>4s}  {"mean":>8s}  {"std":>8s}  {"min":>8s}  {"max":>8s}')
    for row in report["separation"]:
        print(
            f'  {row["embedder"]:30s} {row["pair_set"]:30s} '
            f'{row["n_pairs"]:4d}  {row["mean_cosine"]:8.4f}  {row["stdev"]:8.4f}  '
            f'{row["min"]:8.4f}  {row["max"]:8.4f}'
        )
    print()

    print("--- MARGINS (related minus unrelated; positive = separates) ---")
    for row in report["margins"]:
        print(
            f'  {row["embedder"]:30s} '
            f'crosslingual={row["related_crosslingual"]:+.4f}  '
            f'monolingual={row["related_monolingual"]:+.4f}'
        )
    print()

    print("--- RETRIEVAL PROBE (EvidenceRecall@k; identical corpus/pins/k) ---")
    for row in report["retrieval"]:
        print(
            f'  {row["probe"]:30s} {row["embedder"]:30s} '
            f'k={row["k"]} n={row["n_cases"]} recall={row["evidence_recall"]:.4f}'
        )
    print()

    print("--- COST TABLE ---")
    for row in report["cost"]:
        print(
            f'  {row["endpoint"]:30s} calls={row["calls"]:3d} vectors={row["vectors"]:4d} '
            f'tokens={row["prompt_tokens"]:6d} cost_usd={row["cost_usd"]:.8f} '
            f'metered={row["cost_is_metered"]}'
        )
    total = sum(c["cost_usd"] for c in report["cost"])
    print(f'  {"":30s} {"":>15s} {"":>9s} {"":>6s} {"TOTAL":>14s} {total:.8f}')
    print()

    print("--- FAILURE RATES ---")
    for row in report["failure_rates"]:
        print(
            f'  {row["endpoint"]:30s} calls={row["calls"]:3d} failed={row["failed_calls"]:3d} '
            f'retries={row["retries"]:3d} failure_rate={row["failure_rate"]:.4f}'
        )
    print()

    print("--- LEG STATUS ---")
    print(f'  full_run:              {report["full_run"]}')
    print(f'  legs_not_run:          {report["legs_not_run"]}')
    print(f'  hotpot_cases_skipped:  {report["hotpot_cases_skipped"]}')
    print(f'  coordinator_bound_bge_m3: {report["coordinator_bound_bge_m3"]}')
    print()
    print(f"  TOTAL SPEND: ${total:.8f}")
    print(f"  Report saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
