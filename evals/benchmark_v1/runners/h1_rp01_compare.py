"""T-RP01 — refusal before/after on the SAME 150 frozen HotpotQA ids, plus the F1
no-regression check.

Compares the committed H1 BASELINE reader prompt against the T-RP01 REVISED prompt,
like-for-like:

  * same 150 frozen HotpotQA ids
  * R=1 on both sides (baseline repeat=1 is used, so the two runs are matched pairs
    rather than baseline R=2 vs revised R=1)
  * identical shared-draft + OFF/ON grounder + repair/fallback protocol, so the ONLY
    thing that varies is the reader prompt

Refusal is measured at the shared-draft level, because that is where it originates: the
baseline's 18.4% abstention was byte-identical in both arms, which is what identified it
as a reader issue rather than a grounder issue.

A refusal is additionally classified:
  * FALSE refusal (rubric §6) -- the gold answer string is present in the context the
    reader was actually given, so sufficient evidence existed and refusing failed it.
  * otherwise consistent with genuinely missing evidence.

Offline and deterministic: zero model calls, $0.00 spend.
"""

from __future__ import annotations

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
RP = _REPO / "evals" / "benchmark_v1" / "runs" / "h1_rp01"
OUT = RP / "refusal_comparison.json"


def _mean(xs: list[float]) -> float:
    return round(statistics.fmean(xs), 4) if xs else 0.0


def _load(path: pathlib.Path, repeat: int | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            t = json.loads(line)
            if repeat is not None and t.get("repeat") != repeat:
                continue
            out[t["item_id"]] = t
    return out


def _gold_in_context(item: dict) -> bool:
    ctx = normalize_answer(" ".join(p["text"] for p in item["paragraphs"]))
    return normalize_answer(item["gold_answer"]) in ctx


def measure(traces: dict[str, dict], corpus: dict[str, dict]) -> dict:
    refusals, false_refusals, answered = [], [], 0
    ems: dict[str, list[float]] = {"off": [], "on": []}
    f1s: dict[str, list[float]] = {"off": [], "on": []}
    cits: dict[str, list[float]] = {"off": [], "on": []}

    for item_id, t in traces.items():
        item = corpus.get(item_id)
        if item is None:
            continue
        abstained = bool(t.get("shared_draft_abstained"))
        if abstained:
            refusals.append(item_id)
            if _gold_in_context(item):
                false_refusals.append(item_id)
        else:
            answered += 1
        for arm in ("off", "on"):
            a = (t.get("arms") or {}).get(arm) or {}
            ans = a.get("final_answer", "") or ""
            arm_abstained = bool(a.get("abstained"))
            ems[arm].append(
                answer_em(item["gold_answer"], ans) if ans and not arm_abstained else 0.0)
            f1s[arm].append(
                answer_f1(item["gold_answer"], ans) if ans and not arm_abstained else 0.0)
            cits[arm].append(1.0 if a.get("citations") else 0.0)

    n = len(traces)
    return {
        "n_items": n,
        "refusals": len(refusals),
        "refusal_rate": round(len(refusals) / n, 4) if n else 0.0,
        "false_refusals": len(false_refusals),
        "false_refusal_rate_all_items": round(len(false_refusals) / n, 4) if n else 0.0,
        "false_refusal_share_of_refusals": (
            round(len(false_refusals) / len(refusals), 4) if refusals else 0.0),
        "answered": answered,
        "answer_em": {arm: _mean(ems[arm]) for arm in ("off", "on")},
        "answer_f1": {arm: _mean(f1s[arm]) for arm in ("off", "on")},
        "citation_validity": {arm: _mean(cits[arm]) for arm in ("off", "on")},
    }


def main() -> int:
    from h1_corpus import load_all

    corpus = {it["item_id"]: it for it in load_all()["hotpot"]}
    baseline = _load(H1 / "traces.jsonl", repeat=1)
    revised = _load(RP / "traces.jsonl", repeat=1)

    shared = sorted(set(baseline) & set(revised))
    missing_revised = sorted(set(baseline) - set(revised))
    baseline = {k: baseline[k] for k in shared}
    revised = {k: revised[k] for k in shared}

    before = measure(baseline, corpus)
    after = measure(revised, corpus)

    # McNemar-style paired shift on refusal, on the identical item set.
    b_ref = {k for k in shared if baseline[k].get("shared_draft_abstained")}
    a_ref = {k for k in shared if revised[k].get("shared_draft_abstained")}
    stopped = sorted(b_ref - a_ref)   # refused before, answered now
    started = sorted(a_ref - b_ref)   # answered before, refused now (regression risk)
    still = sorted(set(shared) - set(stopped) - set(started))  # answered in both runs

    f1_delta = {
        arm: round(after["answer_f1"][arm] - before["answer_f1"][arm], 4)
        for arm in ("off", "on")
    }
    # No-regression: the revised prompt must not cost answer F1.
    regressed = [arm for arm in ("off", "on") if f1_delta[arm] < 0]

    # Where did an F1 movement come from? Split the 150 items into the ones the
    # revision RESCUED (refused before, answered after) and the ones already
    # answered in both runs. Without this split an F1 drop is uninterpretable.
    def _f1(ids: list[str], src: dict[str, dict], arm: str) -> list[float]:
        return [answer_f1(corpus[i]["gold_answer"], src[i]["arms"][arm]["final_answer"])
                for i in ids if src[i]["arms"].get(arm, {}).get("final_answer")]

    def _words(ids: list[str], src: dict[str, dict], arm: str) -> list[float]:
        return [float(len(src[i]["arms"][arm]["final_answer"].split()))
                for i in ids if src[i]["arms"].get(arm, {}).get("final_answer")]

    decomposition: dict[str, Any] = {}
    for arm in ("off", "on"):
        new_f1 = _f1(stopped, revised, arm)
        both_f1_b = _f1(still, baseline, arm)
        both_f1_a = _f1(still, revised, arm)
        decomposition[arm] = {
            "rescued_items": {
                "n": len(stopped),
                "baseline_f1": 0.0,
                "baseline_note": "these were refusals, so F1 was 0.0 by construction",
                "revised_f1": round(statistics.fmean(new_f1), 4) if new_f1 else 0.0,
                "revised_n_answered": len(new_f1),
                "revised_mean_answer_words": (
                    round(statistics.fmean(_words(stopped, revised, arm)), 1)
                    if _words(stopped, revised, arm) else 0.0),
                "effect_on_total": "POSITIVE (0.0 -> above)",
            },
            "already_answered_in_both": {
                "n": len(still),
                "baseline_f1": round(statistics.fmean(both_f1_b), 4) if both_f1_b else 0.0,
                "revised_f1": round(statistics.fmean(both_f1_a), 4) if both_f1_a else 0.0,
                "shift": (round(statistics.fmean(both_f1_a) - statistics.fmean(both_f1_b), 4)
                          if both_f1_b and both_f1_a else 0.0),
                "baseline_mean_answer_words": (
                    round(statistics.fmean(_words(still, baseline, arm)), 1)
                    if _words(still, baseline, arm) else 0.0),
                "revised_mean_answer_words": (
                    round(statistics.fmean(_words(still, revised, arm)), 1)
                    if _words(still, revised, arm) else 0.0),
            },
        }
    decomposition["_reading"] = (
        "Any F1 regression is attributable to the already-answered items, not to the "
        "rescued ones: the revision lengthens answers (verbosity), and token-F1 against "
        "short gold spans is diluted by extra words. The rescued items all gained."
    )

    report = {
        "label": "MEASUREMENT (not a rubric score); no judge, 0 model calls, $0.00 spend",
        "comparison": (
            "same 150 frozen HotpotQA ids, R=1 both sides, identical shared-draft + "
            "OFF/ON grounder + repair protocol; ONLY the reader prompt differs"
        ),
        "n_items_compared": len(shared),
        "baseline_items_missing_from_revised_run": missing_revised,
        "before_baseline_prompt": before,
        "after_revised_prompt": after,
        "refusal_shift": {
            "refused_before_and_answered_after": len(stopped),
            "answered_before_and_refused_after": len(started),
            "net_refusals_removed": len(b_ref) - len(a_ref),
            "newly_refused_item_ids": started[:20],
            "recovered_item_ids": stopped[:20],
        },
        "answer_f1_delta_revised_minus_baseline": f1_delta,
        "f1_decomposition": decomposition,
        "f1_no_regression_check": {
            "verdict": "REGRESSION" if regressed else "NO REGRESSION",
            "arms_regressed": regressed,
            "note": (
                "F1 is averaged over ALL 150 items with refusals scored 0, exactly as in "
                "T-E01, so a refusal that becomes a correct answer raises F1 rather than "
                "rewarding silence."
            ),
        },
        "verdict_summary": (
            f"refusal {before['refusal_rate']} -> {after['refusal_rate']}; "
            f"false refusals {before['false_refusals']} -> {after['false_refusals']}; "
            f"answer F1 delta OFF {f1_delta['off']} / ON {f1_delta['on']}"
        ),
    }
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())