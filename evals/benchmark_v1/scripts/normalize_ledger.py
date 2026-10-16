"""Repair `reports/budget_ledger.csv` into ONE parseable schema, losslessly.

Three writers had appended to the same file with three different column layouts
under a single 20-column header:

  * P02 preflight  -> 20 cols (the original header)
  * T-E01 H1        -> 15 cols (metered calls)
  * h2-index        -> 4 cols  (COST PROJECTIONS, not metered spend)

Concatenating different schemas under one header makes the file invalid CSV, so
no downstream tool can sum it. This normaliser maps all three blocks into one
superset schema, adds a `row_type` column so projections can never be mistaken
for spend, and preserves every original value (extras are kept verbatim in
`detail`). The raw file is backed up before the rewrite.

Nothing is dropped, summed away, or re-interpreted.
"""

from __future__ import annotations

import csv
import json
import pathlib
import shutil

REPO = pathlib.Path(__file__).resolve().parents[3]
LEDGER = REPO / "evals" / "benchmark_v1" / "reports" / "budget_ledger.csv"
BACKUP = REPO / "evals" / "benchmark_v1" / "reports" / "budget_ledger.raw_mixed_schema.csv"

FIELDS = [
    "timestamp_utc", "run_tag", "row_type", "call_site",
    "model_requested", "model_used",
    "calls", "free_calls", "paid_calls", "fallbacks", "failed_calls",
    "prompt_tokens", "completion_tokens", "cost_usd", "latency_ms", "error",
    "detail",
]

H1_MAP = {
    "timestamp_utc": 0, "run_tag": 1, "call_site": 2, "model_requested": 3,
    "model_used": 4, "calls": 5, "free_calls": 6, "paid_calls": 7,
    "fallbacks": 8, "failed_calls": 9, "prompt_tokens": 10,
    "completion_tokens": 11, "cost_usd": 12, "latency_ms": 13, "error": 14,
}


def _row(r: list[str], n: int) -> dict[str, str]:
    out = {f: "" for f in FIELDS}

    def at(i: int) -> str:
        return r[i] if i < len(r) else ""

    if n == 20:  # P02 metered call
        out["timestamp_utc"] = at(0)
        out["call_site"] = at(1)
        out["model_requested"] = at(6)
        out["model_used"] = at(7)
        out["calls"] = at(10)
        out["fallbacks"] = at(11)
        out["failed_calls"] = at(12)
        out["prompt_tokens"] = at(13)
        out["completion_tokens"] = at(14)
        out["cost_usd"] = at(17) or str(float(at(15) or 0) + float(at(16) or 0))
        out["latency_ms"] = at(18)
        out["error"] = at(19)
        out["detail"] = json.dumps({
            "item_type": at(2), "n_docs": at(3), "provenance": at(4),
            "embed_model": at(5), "embed_calls": at(8),
            "embed_prompt_tokens": at(9),
            "embedding_cost_usd": at(15), "generation_cost_usd": at(16),
        })
        out["run_tag"] = "p02_preflight"
        out["row_type"] = "metered_call"
    elif n == 15:  # T-E01 H1 metered call
        for k, i in H1_MAP.items():
            out[k] = at(i)
        out["row_type"] = "metered_call"
    elif n == 4:  # corpus-indexing COST PROJECTION (not spend)
        out["run_tag"] = at(0)
        out["row_type"] = "cost_projection"
        out["cost_usd"] = at(3)
        out["detail"] = json.dumps({"cumulative_units": at(1), "per_call_unit": at(2)})
    else:
        out["row_type"] = "unrecognised"
        out["detail"] = json.dumps({"raw": r})
    return out


def main() -> int:
    if not LEDGER.exists():
        print("ledger missing")
        return 1
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    rows, counts = [], {}
    for line in lines[1:]:
        r = next(csv.reader([line]))
        nrow = _row(r, len(r))
        key = (nrow["run_tag"], nrow["row_type"])
        counts[key] = counts.get(key, 0) + 1
        rows.append(nrow)

    if BACKUP.exists():
        BACKUP.unlink()
    shutil.copy2(LEDGER, BACKUP)

    with LEDGER.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    spend = sum(float(r["cost_usd"] or 0) for r in rows if r["row_type"] == "metered_call")
    proj = sum(float(r["cost_usd"] or 0) for r in rows if r["row_type"] == "cost_projection")
    print(f"backup -> {BACKUP.relative_to(REPO)}")
    print(f"rows preserved: {len(rows)}")
    for (tag, kind), n in sorted(counts.items()):
        print(f"  {tag:16s} {kind:15s} {n:5d} rows")
    print(f"real metered spend:   ${spend:.8f}")
    print(f"projections (NOT spend): ${proj:.8f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())