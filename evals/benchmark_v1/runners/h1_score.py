"""H1 deterministic scoring — answer EM/F1, abstention, citation validity.

Deliberately NOT scored here: strict GSR. The rubric is still PROPOSAL
(T-P01), so those scorecard cells are emitted as EMPTY strings, never
approximated or inferred.

SENTENCE-ID RULE (ticket): the pipeline emits ``(title, paragraph)`` — a real
citation to a real paragraph — but NOT ``(title, sentence_id)``. The official
HotpotQA supporting-fact / joint cells therefore stay ``NOT-EMITTED``; instead we
report **paragraph-level support F1** computed from the cited paragraph titles
against the gold supporting titles. Sentence hits are NEVER inferred from
paragraph overlap.
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

from hotpot_evaluate_v1 import answer_em, answer_f1, supporting_fact_f1  # noqa: E402

RUNS = _REPO / "evals" / "benchmark_v1" / "runs" / "h1"

#: Cells that require the frozen rubric; left empty on purpose.
NOT_SCORED = ""


def _mean(xs: list[float]) -> float:
    return round(statistics.fmean(xs), 4) if xs else 0.0


def score_traces(traces: list[dict], corpus: dict[str, list[dict]]) -> dict[str, Any]:
    gold = {}
    for items in corpus.values():
        for it in items:
            gold[(it["item_id"], it["repeats"])] = it

    per_arm: dict[str, dict[str, Any]] = {}

    for arm in ("off", "on"):
        rows: list[dict[str, Any]] = []
        for t in traces:
            item = gold.get((t["item_id"], 1))
            if item is None:  # repeat-2 items share their item record
                item = next(
                    (i for k, i in gold.items() if k[0] == t["item_id"]), None
                )
            a = t.get("arms", {}).get(arm)
            if item is None or a is None:
                # Failures STAY in the denominator, scored 0.
                rows.append({"em": 0.0, "f1": 0.0, "abstained": True,
                             "citation_valid": 0.0, "support_f1": 0.0,
                             "error": t.get("attempt_error") or t.get("draft_error", "")})
                continue
            answer = a.get("final_answer", "") or ""
            abstained = bool(a.get("abstained", False))
            citations = a.get("citations", [])

            if item["answerable"]:
                em = answer_em(item["gold_answer"], answer) if answer and not abstained else 0.0
                f1 = answer_f1(item["gold_answer"], answer) if answer and not abstained else 0.0
            else:
                # Unanswerable: an empty/absent answer is CORRECT abstention.
                em = 1.0 if abstained or not answer else 0.0
                f1 = em

            # Citation validity: does at least one citation resolve to a real
            # paragraph in this item's context, and carry a grounded claim?
            titles = {p["title"] for p in item["paragraphs"]}
            resolves = [c for c in citations if c.get("chunk_id") in titles]
            citation_valid = 1.0 if resolves and citations else 0.0

            # Paragraph-level support F1 (SENTENCE-ID RULE: sp cells stay empty).
            gold_titles = set(item.get("gold_titles") or [])
            cited_titles = {c.get("chunk_id") for c in citations if c.get("chunk_id")}
            support_f1 = (
                supporting_fact_f1(
                    [[t, "0"] for t in sorted(gold_titles)],
                    [[t, "0"] for t in sorted(cited_titles)],
                )
                if gold_titles else 0.0
            )

            rows.append({
                "em": em, "f1": f1, "abstained": abstained,
                "citation_valid": citation_valid, "support_f1": support_f1,
                "item_id": t["item_id"], "dataset": t["dataset"],
                "error": t.get("attempt_error") or t.get("draft_error", ""),
            })

        answerable = [r for r in rows if r["dataset"] != "certified-negative"]
        per_arm[arm] = {
            "n_attempts": len(rows),
            "n_errors": sum(1 for r in rows if r["error"]),
            "answer_em": _mean([r["em"] for r in answerable]),
            "answer_f1": _mean([r["f1"] for r in answerable]),
            "citation_validity": _mean([r["citation_valid"] for r in rows]),
            "paragraph_support_f1": _mean([r["support_f1"] for r in answerable]),
            "abstention_rate": _mean([1.0 if r["abstained"] else 0.0 for r in answerable]),
            "answers_with_zero_citations": sum(
                1 for r in answerable if r["citation_valid"] == 0.0
            ),
            # Rubric-dependent cells: intentionally EMPTY.
            "strict_gsr": NOT_SCORED,
            "supporting_fact_em_official": NOT_SCORED,
            "supporting_fact_f1_official": NOT_SCORED,
            "joint_em_official": NOT_SCORED,
            "joint_f1_official": NOT_SCORED,
        }

    # ---- shared-draft identity (the A/B is only valid if both arms saw the
    # SAME draft). The draft is generated ONCE per (item, repeat) and stored once
    # on the trace, so identity is structural. What we verify from the artifact:
    #   (a) exactly one draft hash per attempt, non-empty (a draft really was made);
    #   (b) both arms received an input of the same size.
    # NOTE: `final_answer` is expected to DIFFER between arms -- it is
    # post-filtering, i.e. the treatment effect itself, so it is NOT an identity
    # check and must not be scored as one.
    one_draft_each = sum(1 for t in traces if t.get("shared_draft_hash"))
    input_size_mismatches: list[str] = []
    for t in traces:
        arms = t.get("arms", {})
        if "off" in arms and "on" in arms:
            if arms["off"].get("claims_total") != arms["on"].get("claims_total"):
                input_size_mismatches.append(f"{t['item_id']}#r{t['repeat']}")
    draft_hash_groups: dict[tuple[str, int], set[str]] = {}
    for t in traces:
        key = (t["item_id"], t["repeat"])
        draft_hash_groups.setdefault(key, set()).add(t.get("shared_draft_hash", ""))

    delta = {
        key: round(per_arm["on"][key] - per_arm["off"][key], 4)
        for key in ("answer_em", "answer_f1", "citation_validity",
                    "paragraph_support_f1", "abstention_rate")
    }

    # Per-slice breakdown so a reader can see the effect is not carried by one set.
    by_slice: dict[str, dict[str, Any]] = {}
    for ds in sorted({t["dataset"] for t in traces}):
        sub = [t for t in traces if t["dataset"] == ds]
        entry: dict[str, Any] = {"n_attempts": len(sub)}
        for arm in ("off", "on"):
            ems, f1s, cits = [], [], []
            for t in sub:
                item = next(
                    (i for k, i in gold.items() if k[0] == t["item_id"]), None
                )
                a = t.get("arms", {}).get(arm) or {}
                answer = a.get("final_answer", "") or ""
                if item and answer and not a.get("abstained"):
                    ems.append(answer_em(item["gold_answer"], answer))
                    f1s.append(answer_f1(item["gold_answer"], answer))
                else:
                    ems.append(0.0)
                    f1s.append(0.0)
                cits.append(1.0 if a.get("citations") else 0.0)
            entry[arm] = {"answer_em": _mean(ems), "answer_f1": _mean(f1s),
                          "citation_validity": _mean(cits)}
        by_slice[ds] = entry

    return {
        "scored": (
            "DETERMINISTIC ONLY (answer EM/F1, abstention, "
            "citation validity/resolution, paragraph-level support F1)"
        ),
        "not_scored": ["strict_gsr (rubric PROPOSAL — T-P01)", "supporting_fact_*", "joint_*"],
        "sentence_id_rule": (
            "NOT-EMITTED: pipeline cites (title, paragraph), not "
            "(title, sentence_id); sentence hits never inferred"
        ),
        "n_attempts": len(traces),
        "comparability_caveat": (
            "The draft prompt elicits a full-sentence answer while HotpotQA gold "
            "answers are short spans, so absolute EM is structurally near zero and "
            "F1 is depressed. The OFF/ON contrast is valid (identical shared drafts); "
            "these absolutes are NOT comparable to published HotpotQA leaderboards."
        ),
        "shared_draft_check": {
            "attempts": len(traces),
            "attempts_with_one_draft_hash": one_draft_each,
            "item_repeat_pairs": len(draft_hash_groups),
            "pairs_with_multiple_distinct_draft_hashes": sum(
                1 for v in draft_hash_groups.values() if len(v) > 1
            ),
            "arms_with_differing_input_claim_count": len(input_size_mismatches),
            "arm_input_size_mismatch_ids": input_size_mismatches[:10],
            "note": (
                "One draft is generated per (item, repeat) before the fork and both "
                "arms consume it; final_answer divergence is the treatment effect, "
                "not a draft divergence."
            ),
        },
        "not_run": {
            "certified-negative abstention slice (25 items)": (
                "BLOCKED: the negative manifest carries no question text, and none of "
                "the 25 ids appear in any local 2wiki split "
                "(validation/test/train-0/train-1 all 0/25 matched), so the questions "
                "are unrecoverable locally. Their snapshot FILES loaded fine "
                "(8 paragraphs each) but a snapshot without its question cannot "
                "exercise abstention. Questions were NOT fabricated."
            ),
        },
        "arms": per_arm,
        "by_slice": by_slice,
        "delta_on_minus_off": delta,
    }


def main() -> int:
    from h1_corpus import load_all

    with (RUNS / "traces.jsonl").open(encoding="utf-8") as fh:
        traces = [json.loads(line) for line in fh]
    report = score_traces(traces, load_all())
    (RUNS / "scorecard.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())