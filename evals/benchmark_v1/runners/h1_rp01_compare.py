"""T-RP01b — three-way comparison of reader-prompt variants on the SAME 150 ids.

Compares, all at R=1 on the identical 150 frozen HotpotQA ids with an identical
shared-draft + OFF/ON grounder + repair protocol, so the ONLY varying factor is the
reader prompt:

  baseline : the H1 control prompt (runs/h1, repeat 1)
  revised  : T-RP01 narrowed-refusal prompt (runs/h1_rp01)
  capped   : T-RP01b narrowed refusal MINUS elaboration PLUS a 20-word answer cap
             (runs/h1_rp01b)

Reports refusal, false refusal, answer EM/F1 and citation validity per arm against BOTH
prior variants, plus mean answer length -- the mechanism T-RP01 identified as the cause
of the ON-arm F1 regression.

"Length back in band" is judged against a band derived from the BASELINE's own measured
mean, +/-10%, computed in-script. It is not a threshold chosen to flatter the result.

Offline and deterministic: zero model calls, $0.00 spend.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from typing import Any

_REPO = pathlib.Path(__file__).resolve().parents[3]
for _p in (_REPO / "evals" / "vendor", str(_REPO / "evals" / "benchmark_v1" / "runners")):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hotpot_evaluate_v1 import answer_em, answer_f1, normalize_answer  # noqa: E402

H1 = _REPO / "evals" / "benchmark_v1" / "runs" / "h1"
RP01 = _REPO / "evals" / "benchmark_v1" / "runs" / "h1_rp01"
RP01B = _REPO / "evals" / "benchmark_v1" / "runs" / "h1_rp01b"

#: Half-width of the "length back in band" tolerance, applied to the baseline mean.
BAND_TOLERANCE = 0.10


def _mean(xs: list[float]) -> float:
    return round(statistics.fmean(xs), 4) if xs else 0.0


def _load(path: pathlib.Path, repeat: int = 1) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            t = json.loads(line)
            if t.get("repeat") == repeat:
                out[t["item_id"]] = t
    return out


def _gold_in_context(item: dict) -> bool:
    ctx = normalize_answer(" ".join(p["text"] for p in item["paragraphs"]))
    return normalize_answer(item["gold_answer"]) in ctx


def measure(traces: dict[str, dict], corpus: dict[str, dict]) -> dict[str, Any]:
    refusals: list[str] = []
    false_refusals: list[str] = []
    answered = 0
    ems: dict[str, list[float]] = {"off": [], "on": []}
    f1s: dict[str, list[float]] = {"off": [], "on": []}
    cits: dict[str, list[float]] = {"off": [], "on": []}
    words: dict[str, list[float]] = {"off": [], "on": []}

    for item_id, t in traces.items():
        item = corpus.get(item_id)
        if item is None:
            continue
        if t.get("shared_draft_abstained"):
            refusals.append(item_id)
            if _gold_in_context(item):
                false_refusals.append(item_id)
        else:
            answered += 1
        for arm in ("off", "on"):
            a = (t.get("arms") or {}).get(arm) or {}
            ans = a.get("final_answer", "") or ""
            abst = bool(a.get("abstained"))
            ems[arm].append(
                answer_em(item["gold_answer"], ans) if ans and not abst else 0.0)
            f1s[arm].append(
                answer_f1(item["gold_answer"], ans) if ans and not abst else 0.0)
            cits[arm].append(1.0 if a.get("citations") else 0.0)
            if ans:
                words[arm].append(float(len(ans.split())))

    n = len(traces)
    return {
        "n_items": n,
        "refusals": len(refusals),
        "refusal_rate": round(len(refusals) / n, 4) if n else 0.0,
        "false_refusals": len(false_refusals),
        "answered": answered,
        "answer_em": {arm: _mean(ems[arm]) for arm in ("off", "on")},
        "answer_f1": {arm: _mean(f1s[arm]) for arm in ("off", "on")},
        "citation_validity": {arm: _mean(cits[arm]) for arm in ("off", "on")},
        "mean_answer_words": {arm: _mean(words[arm]) for arm in ("off", "on")},
        "n_answers_counted_for_words": {arm: len(words[arm]) for arm in ("off", "on")},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(RP01B / "variant_comparison.json"))
    args = ap.parse_args()

    from h1_corpus import load_all

    corpus = {it["item_id"]: it for it in load_all()["hotpot"]}
    raw = {
        "baseline": _load(H1 / "traces.jsonl"),
        "revised": _load(RP01 / "traces.jsonl"),
        "capped": _load(RP01B / "traces.jsonl"),
    }
    # Restrict every variant to the ids ALL variants share, so the three-way
    # comparison is on an identical item set.
    shared = sorted(set.intersection(*(set(v) for v in raw.values())))
    variants = {k: {i: v[i] for i in shared} for k, v in raw.items()}
    measured = {k: measure(v, corpus) for k, v in variants.items()}

    # Length band, anchored on the baseline's own measured ON-arm mean.
    base_words = measured["baseline"]["mean_answer_words"]["on"]
    band_lo = round(base_words * (1 - BAND_TOLERANCE), 2)
    band_hi = round(base_words * (1 + BAND_TOLERANCE), 2)

    def in_band(words: float) -> bool:
        return band_lo <= words <= band_hi

    length = {
        arm: {
            "baseline": measured["baseline"]["mean_answer_words"][arm],
            "revised": measured["revised"]["mean_answer_words"][arm],
            "capped": measured["capped"]["mean_answer_words"][arm],
            "band": [band_lo, band_hi],
            "capped_in_band": in_band(measured["capped"]["mean_answer_words"][arm]),
            "revised_in_band": in_band(measured["revised"]["mean_answer_words"][arm]),
            "capped_minus_baseline": round(
                measured["capped"]["mean_answer_words"][arm]
                - measured["baseline"]["mean_answer_words"][arm], 2),
        }
        for arm in ("off", "on")
    }

    def deltas(variant: str, ref: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for arm in ("off", "on"):
            out[arm] = {
                m: round(measured[variant][m][arm] - measured[ref][m][arm], 4)
                for m in ("answer_em", "answer_f1", "citation_validity")
            }
            out[arm]["refusal_rate"] = round(
                measured[variant]["refusal_rate"] - measured[ref]["refusal_rate"], 4)
            out[arm]["mean_answer_words"] = round(
                measured[variant]["mean_answer_words"][arm]
                - measured[ref]["mean_answer_words"][arm], 2)
        return out

    # Ticket's verdict question, evaluated explicitly.
    refusal_held = measured["capped"]["refusal_rate"] <= measured["revised"]["refusal_rate"]
    length_back = length["on"]["capped_in_band"] and length["off"]["capped_in_band"]
    f1_recovered = {
        arm: measured["capped"]["answer_f1"][arm] >= measured["baseline"]["answer_f1"][arm]
        for arm in ("off", "on")
    }
    verdict = (
        "VERDICT: verbosity back in band AND refusal staying fixed"
        if (length_back and refusal_held)
        else "VERDICT: NOT both -- see length_back_in_band / refusal_holding_fixed"
    )

    report = {
        "label": "MEASUREMENT (not a rubric score); no judge, 0 model calls, $0.00 spend",
        "comparison": (
            "same 150 frozen HotpotQA ids, R=1, identical shared-draft + OFF/ON grounder "
            "+ repair protocol across all three variants; ONLY the reader prompt differs"
        ),
        "n_items_compared": len(shared),
        "variants": measured,
        "mean_answer_words_by_variant": length,
        "length_band_definition": (
            f"baseline ON mean {base_words} words +/-{int(BAND_TOLERANCE * 100)}% "
            f"=> [{band_lo}, {band_hi}] words"
        ),
        "deltas": {
            "capped_vs_baseline": deltas("capped", "baseline"),
            "capped_vs_revised": deltas("capped", "revised"),
        },
        "ticket_verdict": {
            "refusal_holding_fixed": refusal_held,
            "length_back_in_band": length_back,
            "answer_f1_at_or_above_baseline": f1_recovered,
            "verdict": verdict,
        },
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())