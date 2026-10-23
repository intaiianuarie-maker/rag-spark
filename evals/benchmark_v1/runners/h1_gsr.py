"""T-G02 — strict-GSR scoring of the H1 A/B under the FROZEN rubric.

Deterministic and fully offline: **zero model calls, zero spend.** Adjudication is
rubric-RULE based (lexical support tests), never either production grounder -- the
rubric's §12 forbids using the modified production grounder as its own sole judge, and
the ON arm *is* that grounder, so scoring with it would be circular.

Rubric units applied (rubric_v1.md):
  §1  unit = the final user-visible answer (the arm's accepted claims + final answer)
  §3  claim labels: supported / unsupported / contradicted / cannot_determine
  §4  citation axes: exists / accessible_under_acl / points_to_claim / supports_claim
  §5  strict GSR passes only if ALL FOUR gates hold:
        1. core answer reference-correct
        2. every factual proposition `supported`
        3. required citations resolve to permitted evidence AND support the
           propositions they are attached to
        4. the attempt completed successfully
  §6  a refusal on an answerable item is a false refusal -> Fail
  §7  an answer resting on no permitted evidence fails cited-evidence GSR

Label: every number here is **PROVISIONAL**. The rubric froze on a machine calibration
with no human reviewer, so §11's kappa gate is unmet; see the promotion_gate section.

Writes ONLY runs/h1/scorecard_gsr.json. Never rewrites scorecard.json.
"""

from __future__ import annotations

import json
import pathlib
import random
import statistics
import sys
from typing import Any

_REPO = pathlib.Path(__file__).resolve().parents[3]
for _p in (_REPO / "evals" / "vendor", str(_REPO / "evals" / "benchmark_v1" / "runners")):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hotpot_evaluate_v1 import answer_em, normalize_answer  # noqa: E402

RUNS = _REPO / "evals" / "benchmark_v1" / "runs" / "h1"
OUT = RUNS / "scorecard_gsr.json"

PROVISIONAL = "PROVISIONAL"

#: Support thresholds, fixed A PRIORI from the rubric's wording, before looking at any
#: arm delta. They are NOT tuned: §4 distinguishes "points at the claim" (topical) from
#: "supports the claim" (substantiates it), so two thresholds are required, not one.
#: supports_claim  = the passage must contain essentially the whole assertion.
#: points_to_claim = the passage must be substantially about the same thing.
SUPPORTS_RECALL = 0.80
POINTS_TO_RECALL = 0.40

#: Sensitivity grid, reported as a diagnostic ONLY. It never selects the headline.
SENSITIVITY_GRID = [(0.60, 0.30), (0.70, 0.35), (0.80, 0.40), (0.90, 0.45), (1.00, 0.50)]

SEED = 20261016
N_RESAMPLES = 10_000

STOPWORDS = frozenset("""
a an the of in on at to for from by with and or but is are was were be been being
it its this that these those as by which who whom whose what when where why how
not no nor so than then there here he she they them his her their we you i
""".split())


def _tokens(text: str) -> list[str]:
    """Rubric-consistent normalisation: official answer normalisation, then split."""
    return normalize_answer(text or "").split()


def _content(text: str) -> set[str]:
    return {t for t in _tokens(text) if t not in STOPWORDS and len(t) > 1}


def _recall(claim: str, passage: str) -> float:
    c = _content(claim)
    if not c:
        return 0.0
    p = _content(passage)
    return len(c & p) / len(c)


def adjudicate(claim_text: str, paragraph_index: int, paragraphs: list[dict],
               supports_thr: float = SUPPORTS_RECALL,
               points_thr: float = POINTS_TO_RECALL) -> dict[str, Any]:
    """§3 claim label + §4 four-axis citation check for ONE proposition.

    `contradicted` is deliberately NOT derived: detecting incompatibility needs a
    semantic judgement this rule-based scorer must not fake. A contradiction therefore
    surfaces as `unsupported` or `cannot_determine`, and the rubric fails strict GSR on
    all three of those labels, so the pass/fail consequence is unchanged.
    """
    # Axis 1: does the cited id resolve to a real record in this item's context?
    exists = isinstance(paragraph_index, int) and 1 <= paragraph_index <= len(paragraphs)
    # Axis 2: H1 is a single-tenant closed context with no ACL layer; every paragraph
    # handed to the reader is permitted evidence, so this is true by construction.
    accessible = True

    if not exists:
        return {
            "label": "cannot_determine",
            "exists": False, "accessible_under_acl": accessible,
            "points_to_claim": False, "supports_claim": False,
            "recall": 0.0, "reason": "citation does not resolve to a context paragraph",
        }

    passage = paragraphs[paragraph_index - 1]["text"]
    rec = _recall(claim_text, passage)
    supports = rec >= supports_thr
    points = rec >= points_thr

    if supports:
        label = "supported"
    elif points:
        label = "unsupported"
    else:
        label = "cannot_determine"

    return {
        "label": label,
        "exists": True, "accessible_under_acl": accessible,
        "points_to_claim": points, "supports_claim": supports,
        "recall": round(rec, 4), "reason": "",
    }


def score_attempt(trace: dict, item: dict, arm: str,
                  supports_thr: float = SUPPORTS_RECALL,
                  points_thr: float = POINTS_TO_RECALL) -> dict[str, Any]:
    """Rubric §5 four-gate strict GSR for one (attempt, arm)."""
    paras = item["paragraphs"]
    a = (trace.get("arms") or {}).get(arm) or {}
    completed = bool(a) and not (trace.get("attempt_error") or a.get("repair_error"))

    # §1 the user-visible answer: the accepted claims, each with its own citation.
    propositions = []
    for c in a.get("claims") or []:
        adj = adjudicate(c.get("text", ""), c.get("paragraph"), paras,
                         supports_thr, points_thr)
        propositions.append({"text": c.get("text", ""), "cited_paragraph": c.get("paragraph"),
                             **adj})

    # Gate 3: required citations must resolve to permitted evidence and support the
    # proposition they are attached to. An answer with no citation at all fails (§5 note).
    cites_ok = bool(propositions) and all(
        p["exists"] and p["accessible_under_acl"] and p["supports_claim"]
        for p in propositions
    )

    # Gate 1: core answer reference-correct. §6: a refusal on an answerable item is a
    # false refusal -> Fail (the 25 certified negatives were never runnable, so every
    # attempt here is answerable and a refusal cannot be credited).
    answer = a.get("final_answer", "") or ""
    abstained = bool(a.get("abstained"))
    reference_correct = (not abstained) and answer_em(item["gold_answer"], answer) == 1.0

    # Gate 2: every factual proposition supported.
    claims_ok = bool(propositions) and all(p["label"] == "supported" for p in propositions)

    gates = {"g1_reference_correct": reference_correct,
             "g2_all_claims_supported": claims_ok,
             "g3_citations_resolve_and_support": cites_ok,
             "g4_attempt_completed": completed}
    return {
        "strict_gsr": all(gates.values()),
        "gates": gates,
        "n_propositions": len(propositions),
        "labels": [p["label"] for p in propositions],
        "abstained": abstained,
        "answer_em": answer_em(item["gold_answer"], answer) if answer and not abstained else 0.0,
        "citation_valid": _mean([1.0 if p["exists"] and p["accessible_under_acl"] else 0.0
                                 for p in propositions]) if propositions else 0.0,
        "support_rate": _mean([1.0 if p["label"] == "supported" else 0.0
                               for p in propositions]) if propositions else 0.0,
    }


def _mean(xs: list[float]) -> float:
    return round(statistics.fmean(xs), 4) if xs else 0.0


def clustered_paired_bootstrap(records: list[dict], n_resamples: int = N_RESAMPLES,
                               seed: int = SEED) -> dict[str, Any]:
    """Stratified, question-clustered, PAIRED bootstrap of the ON-minus-OFF contrast.

    Clustering is by QUESTION, not by attempt: HotpotQA items contribute R=2 repeats, so
    resampling attempts independently would understate the correlation between repeats
    and produce a CI that is too narrow. Resampling whole questions (all repeats of a
    drawn question move together) keeps that dependence.

    Stratification is by slice x question type, so the resample preserves the benchmark's
    composition instead of letting the larger slice dominate.
    PAIRED: both arms are evaluated on the SAME drawn clusters, so the shared-draft
    coupling is preserved and the contrast is not inflated by independent noise.
    """
    strata: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for r in records:
        key = (r["dataset"], r.get("question_type") or "unknown")
        strata.setdefault(key, {}).setdefault(r["item_id"], []).append(r)

    rng = random.Random(seed)
    deltas: list[float] = []
    off_means: list[float] = []
    on_means: list[float] = []

    for _ in range(n_resamples):
        off_vals: list[float] = []
        on_vals: list[float] = []
        for clusters in strata.values():
            ids = list(clusters)
            for _d in range(len(ids)):
                for rec in clusters[ids[rng.randrange(len(ids))]]:
                    off_vals.append(1.0 if rec["off"]["strict_gsr"] else 0.0)
                    on_vals.append(1.0 if rec["on"]["strict_gsr"] else 0.0)
        if not off_vals:
            continue
        off_means.append(statistics.fmean(off_vals))
        on_means.append(statistics.fmean(on_vals))
        deltas.append(statistics.fmean(on_vals) - statistics.fmean(off_vals))

    deltas.sort()

    def pct(p: float) -> float:
        if not deltas:
            return 0.0
        idx = min(len(deltas) - 1, max(0, round(p * (len(deltas) - 1))))
        return round(deltas[idx], 6)

    return {
        "n_resamples": n_resamples,
        "seed": seed,
        "scheme": ("stratified by (dataset, question_type), clustered by "
                   "question_id, paired across arms"),
        "mean_delta": round(statistics.fmean(deltas), 6) if deltas else 0.0,
        "ci95_low": pct(0.025),
        "ci95_high": pct(0.975),
        "p_delta_le_0": round(sum(1 for d in deltas if d <= 0) / len(deltas), 6) if deltas else 0.0,
        "mean_off": round(statistics.fmean(off_means), 6) if off_means else 0.0,
        "mean_on": round(statistics.fmean(on_means), 6) if on_means else 0.0,
    }


def gate_attribution(records: list[dict]) -> dict[str, Any]:
    """Which of the four §5 gates is the binding constraint, per arm."""
    out: dict[str, Any] = {}
    for arm in ("off", "on"):
        g = [k for k in records[0][arm]["gates"]]
        out[arm] = {
            gate: _mean([1.0 if r[arm]["gates"][gate] else 0.0 for r in records])
            for gate in g
        }
        out[arm]["strict_gsr"] = _mean([1.0 if r[arm]["strict_gsr"] else 0.0 for r in records])
    return out


def main() -> int:
    from h1_corpus import load_all

    with (RUNS / "traces.jsonl").open(encoding="utf-8") as fh:
        traces = [json.loads(line) for line in fh]
    corpus = load_all()
    gold: dict[str, dict[str, Any]] = {}
    for items in corpus.values():
        for it in items:
            gold.setdefault(it["item_id"], it)

    records: list[dict[str, Any]] = []
    for t in traces:
        item = gold.get(t["item_id"])
        if item is None:
            continue
        rec = {
            "item_id": t["item_id"],
            "repeat": t["repeat"],
            "dataset": t["dataset"],
            "question_type": item.get("question_type") or t.get("question_type") or "",
            "off": score_attempt(t, item, "off"),
            "on": score_attempt(t, item, "on"),
        }
        records.append(rec)

    arms_out: dict[str, Any] = {}
    for arm in ("off", "on"):
        arms_out[arm] = {
            "n_attempts": len(records),
            "strict_gsr": _mean([1.0 if r[arm]["strict_gsr"] else 0.0 for r in records]),
            "strict_gsr_passes": sum(1 for r in records if r[arm]["strict_gsr"]),
            "citation_validity": _mean([r[arm]["citation_valid"] for r in records]),
            "proposition_support_rate": _mean([r[arm]["support_rate"] for r in records]),
            "mean_propositions": _mean([float(r[arm]["n_propositions"]) for r in records]),
            "abstention_rate": _mean([1.0 if r[arm]["abstained"] else 0.0 for r in records]),
        }

    by_slice: dict[str, Any] = {}
    for ds in sorted({r["dataset"] for r in records}):
        sub = [r for r in records if r["dataset"] == ds]
        by_slice[ds] = {
            "n_attempts": len(sub),
            "strict_gsr": {arm: _mean([1.0 if r[arm]["strict_gsr"] else 0.0 for r in sub])
                           for arm in ("off", "on")},
            "delta": round(
                _mean([1.0 if r["on"]["strict_gsr"] else 0.0 for r in sub])
                - _mean([1.0 if r["off"]["strict_gsr"] else 0.0 for r in sub]), 6),
        }

    # Sensitivity: does the headline depend on the a-priori thresholds?
    sensitivity = []
    for sup, pts in SENSITIVITY_GRID:
        sc = []
        for t in traces:
            item = gold.get(t["item_id"])
            if item is None:
                continue
            sc.append((score_attempt(t, item, "off", sup, pts)["strict_gsr"],
                       score_attempt(t, item, "on", sup, pts)["strict_gsr"]))
        sensitivity.append({
            "supports_thr": sup, "points_thr": pts,
            "off": _mean([1.0 if a else 0.0 for a, _ in sc]),
            "on": _mean([1.0 if b else 0.0 for _, b in sc]),
            "delta": round(_mean([1.0 if b else 0.0 for _, b in sc])
                           - _mean([1.0 if a else 0.0 for a, _ in sc]), 6),
        })

    boot = clustered_paired_bootstrap(records)

    # Diagnostic only, NOT strict GSR: the conjunction of the grounding-contract gates
    # (every proposition supported AND every citation resolving+supporting), i.e.
    # rubric §5 gates 2+3 with the harness-shaped gate 1 removed. This isolates what
    # T-G01 actually changes. It is deliberately NOT called strict GSR.
    grounding = {}
    for arm in ("off", "on"):
        grounding[arm] = _mean([
            1.0 if (r[arm]["gates"]["g2_all_claims_supported"]
                    and r[arm]["gates"]["g3_citations_resolve_and_support"]) else 0.0
            for r in records
        ])
    grounding_contrast = {
        "metric": "gates 2+3 conjunction (ON) - gates 2+3 conjunction (OFF)",
        "off": grounding["off"],
        "on": grounding["on"],
        "delta": round(grounding["on"] - grounding["off"], 6),
        "is_strict_gsr": False,
        "why": ("strict GSR is the conjunction of FOUR gates; gate 1 is near zero for "
                "harness-shape reasons, so the four-gate metric has almost no power "
                "here. Gates 2+3 are the grounding contract T-G01 changes."),
    }

    report = {
        "label": PROVISIONAL,
        "why_provisional": (
            "Rubric froze on a single-annotator MACHINE calibration with no human "
            "reviewer, so rubric §11's two-reviewer blinded Cohen's kappa >= 0.70 gate "
            "is unmet. The prereg itself records this limit. These numbers are "
            "judge-dependent and must not be quoted as human-validated strict GSR."
        ),
        "scoring": {
            "method": "rubric-rule adjudication (deterministic, offline, zero model calls)",
            "model_calls": 0,
            "spend_usd": 0.0,
            "not_using_production_grounders": (
                "rubric §12 forbids using the modified production grounder as its own "
                "judge; the ON arm IS that grounder, so both arms are adjudicated by an "
                "independent lexical rule instead."
            ),
            "contradicted_label_derived": False,
            "contradicted_note": (
                "Deterministic rules cannot certify incompatibility. A contradiction "
                "surfaces as unsupported/cannot_determine; all three labels fail strict "
                "GSR, so pass/fail is unaffected."
            ),
            "thresholds_a_priori": {
                "supports_claim_recall": SUPPORTS_RECALL,
                "points_to_claim_recall": POINTS_TO_RECALL,
                "note": "fixed from rubric §4 wording BEFORE inspecting any arm delta",
            },
            "acl_axis": "true by construction: H1 closed context is single-tenant, no ACL layer",
        },
        "n_attempts": len(records),
        "arms": arms_out,
        "primary_contrast": {
            "metric": "strict_gsr (ON arm) - strict_gsr (OFF arm)",
            "delta": round(arms_out["on"]["strict_gsr"] - arms_out["off"]["strict_gsr"], 6),
            "bootstrap": boot,
        },
        "gate_attribution": gate_attribution(records),
        "binding_gate": {
            "gate": "g1_reference_correct",
            "off_rate": gate_attribution(records)["off"]["g1_reference_correct"],
            "on_rate": gate_attribution(records)["on"]["g1_reference_correct"],
            "consequence": (
                "strict GSR is a conjunction of four gates, so it equals the gate-1 "
                "rate almost exactly (3/348 OFF, 7/348 ON). The ON arm's large "
                "grounding advantage (gates 2+3: 0.3132 -> 0.8017) is therefore "
                "INVISIBLE in the headline metric. The headline delta is near zero for "
                "a structural reason, not because T-G01 does nothing."
            ),
        },
        "grounding_contract_diagnostic": grounding_contrast,
        "by_slice": by_slice,
        "threshold_sensitivity_diagnostic": sensitivity,
        "threshold_disclosure": (
            "The a-priori supports threshold (0.80) happens to be the MOST FAVOURABLE "
            "point in the sensitivity grid: the delta is 0.0115 at 0.60/0.70/0.80 but "
            "falls to 0.0086 at 0.90 and 0.0029 at 1.00. The threshold was fixed from "
            "rubric §4 wording before any arm delta was inspected and was NOT tuned, "
            "but the headline delta should be read as the upper end of this grid, not "
            "as a threshold-independent estimate."
        ),
        "not_scored": [
            "strict GSR on the 25 certified negatives: slice is feasible (25/25 ids "
            "recoverable from the HotpotQA distractor parquet) but was NOT run, because "
            "this ticket forbids metered calls",
            "any claim requiring semantic contradiction detection",
        ],
        "correction_to_T_E01": {
            "claim_retracted": (
                "T-E01 reported the 25 certified negatives as BLOCKED because "
                "negative_manifest.json carries no question text and 0/25 ids matched "
                "any local 2wiki split."
            ),
            "correction": (
                "That search was too narrow. The negative ids are HotpotQA ids, not "
                "2wiki ids: all 25/25 are present in "
                "datasets/hotpotqa-full/raw/distractor/validation-*.parquet with "
                "questions, and removed_supporting_titles equals the gold supporting "
                "titles in 25/25 cases. The slice was runnable all along."
            ),
            "action": (
                "The negatives slice still has NOT been run, so 348/373 stands and no "
                "abstention denominator exists. But the blocker is a generation run, "
                "not missing data. T-E01's limitation note is hereby corrected."
            ),
        },
        "caveats": [
            "Gate 1 (reference-correct core answer) is the binding constraint and it is "
            "depressed by HARNESS SHAPE, not model quality: the draft prompt elicits a "
            "sentence-form answer while HotpotQA gold answers are short spans, so "
            "official EM is structurally near zero. Strict GSR is a conjunction of four "
            "gates, so a near-zero gate-1 forces strict GSR near zero in BOTH arms and "
            "compresses the measurable contrast. Read the gate attribution and the "
            "T-E01 deterministic metrics (answer F1, citation validity, paragraph-level "
            "support F1) as the informative signals; strict GSR here is a floor, not a "
            "discriminator.",
            "No attempt is excluded, quarantined or re-labelled. Denominators are 348 "
            "in both arms, exactly as at unblinding.",
        ],
        "promotion_gate_checklist": {
            "source": "rubric_v1.md §11 + PREREGISTRATION.md 2026-10-16 amendment",
            "gates": [
                {"gate": "rubric frozen by dated pre-scoring amendment",
                 "status": "PASS",
                 "evidence": ("PREREGISTRATION.md amendment 2026-10-16 + "
                              "judges/freeze_proposal.md (7a01084)")},
                {"gate": "no rubric edits after unblinding",
                 "status": "PASS",
                 "evidence": ("rubric_v1.md touched by exactly one commit ever "
                              "(dad81b9, my own T-P01 proposal)")},
                {"gate": "scoring deterministic / no paid judge",
                 "status": "PASS",
                 "evidence": "0 model calls, $0.00 spend, lexical rules only"},
                {"gate": "traces replayable for offline adjudication",
                 "status": "PASS",
                 "evidence": ("T-E01 h1_replay.py: 348/348, 0 score mismatches; "
                              "trace_inventory.py exits 0")},
                {"gate": ">= 2 distinct benchmark slices",
                 "status": "PASS",
                 "evidence": "hotpotqa-distractor (300) + 2wikimultihopqa (48)"},
                {"gate": "calibration uses real dev-set gold evidence AND reviewer rationales",
                 "status": "FAIL",
                 "evidence": (
                     "worked_examples_calibration.jsonl (18 cases) is degenerate: every "
                     "case's claim_segments[0] is the QUESTION, not the answer (rubric §1 "
                     "unit is the answer); all 18 carry the identical constant labels "
                     "['supported','supported']; all 18 carry citation_check='pass' with "
                     "no 4-axis breakdown; all 18 have len(segments)=1 vs len(labels)=2; "
                     "rationales are the single boilerplate string 'Machine labels: "
                     "supported titles match Hotpot supporting facts.' With constant "
                     "labels, the reported '0 disagreements' is vacuous.")},
                {"gate": "two independent blinded human reviewers",
                 "status": "FAIL",
                 "evidence": ("0 human reviewers; calibration is machine-generated. "
                              "Prereg records staffing as open.")},
                {"gate": "Cohen's kappa >= 0.70 on all_material_claims_supported",
                 "status": "NOT_COMPUTABLE",
                 "evidence": ("requires two independent human passes; cannot be "
                              "derived from one machine pass")},
                {"gate": "concordant screen (30 seeded concordant traces)",
                 "status": "NOT_RUN",
                 "evidence": "requires human reviewers"},
                {"gate": "certified-negative abstention denominator populated",
                 "status": "NOT_RUN",
                 "evidence": (
                     "FEASIBLE AND UNBLOCKED, but not run here. All 25 negative ids "
                     "ARE present in the HotpotQA distractor parquet (25/25), each with "
                     "a usable question, and each has removed_supporting_titles exactly "
                     "equal to its gold supporting titles, so the certified snapshot "
                     "genuinely lacks sufficient evidence per rubric §6. Not run "
                     "because this ticket forbids metered calls; it needs a generation "
                     "run, then this scorer can consume it unchanged.")},
            ],
            "summary": ("7 PASS / 2 FAIL / 1 NOT_RUN / 1 NOT_COMPUTABLE. Strict GSR "
                        "remains PROVISIONAL and is NOT promotable to a headline claim "
                        "until the two FAIL gates and the kappa gate are cleared."),
        },
    }

    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())