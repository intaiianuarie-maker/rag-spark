# Rubric v1 — operational claim/citation rubric (PROVISIONAL INSTRUMENT)

**Status: FROZEN SCORING INSTRUMENT — NOT HUMAN-VALIDATED.** Frozen 2026-10-16
(T-J01 rework) on a valid 20-case calibration (4 label classes, 5 archetypes,
answer-segmented, 4-axis citations, 0 holdout overlap). Scores computed under
it are valid instrument outputs but remain PROVISIONAL until two-reviewer
blinded agreement meets the κ≥0.70 gate (staffing open). Drafted by
dev1 for benchmark Phase 1 (T-P01 step 2) from
`RAG-Spark_Benchmark_Plan_v1.4.md` §21. The coordinator owns freezing. Nothing
here may be edited to improve an A/B delta after unblinding;
any change requires a dated preregistration amendment (§10.3).

**Also required before use:** §21.1 states the worked examples below are *logical
illustrations, not gold labels*. Before any untouched-holdout review, this
benchmark must supply separate **real development-set calibration examples**
with gold evidence and reviewer rationales. This file does not satisfy that
requirement and must not be presented as if it did.

---

## 1. Unit of annotation

The scoring unit is the **final user-visible answer**, not the claim and not the
citation. Claim-level labels are recorded as evidence *about* the answer-level
verdict and must be reconciled to it explicitly (§21.3).

Segment the answer into **independently falsifiable factual propositions**,
including:

- the core answer,
- causal / explanatory statements,
- quantities, dates and numbers,
- named entities,
- explicit references to evidence ("according to source-7").

## 2. What counts as a material claim

A proposition is **material** if it asserts, or materially implies, a real-world
fact a reader could rely on — **even when incidental to the short answer**.

Not material: pure politeness, formatting, confidence qualifiers with no factual
assertion ("I believe", "certainly"), and verbatim restatement of the question.

## 3. Claim label schema

Each factual proposition gets exactly one label, assigned **from the permitted
frozen retrieval snapshot and the cited passages — never from the reviewer's
memory of the answer**:

| Label | Meaning |
|---|---|
| `supported` | Established by the permitted evidence. |
| `unsupported` | Asserted but not established by the permitted evidence. |
| `contradicted` | Permitted evidence states something incompatible. |
| `cannot_determine` | Evidence is insufficient to decide. |

**`cannot_determine` fails strict GSR pending adjudication.** It is never
silently credited and never silently re-labelled as `supported`.

## 4. Citation label schema

Each citation is labelled on **four independent axes** — they are not collapsed:

| Axis | Values | Meaning |
|---|---|---|
| `exists` | `true` / `false` | The cited id resolves to a real record. |
| `accessible_under_acl` | `true` / `false` | The caller may read it in their scope. |
| `points_to_claim` | `true` / `false` | The cited material is topically the thing asserted. |
| `supports_claim` | `true` / `false` | The cited material actually substantiates the proposition. |

A real, accessible citation that points at unrelated material is **valid
resolution but invalid support**: `points_to_claim=false`, `supports_claim=false`.

## 5. Strict GSR (answer-level) — the pass rule

An answer **passes strict GSR only if all four hold**:

1. the **core answer is reference-correct**;
2. **every** factual proposition is `supported`;
3. required citations resolve to permitted evidence **and** support the
   propositions they are attached to;
4. the attempt **completed successfully**.

This remains true **after** an unsupported extra sentence follows a correct short
answer — the extra sentence fails the answer.

Note the asymmetry this creates: a correct answer with **no valid required
citation** **fails** strict GSR.

## 6. Negative items (abstention)

A refusal is graded against the frozen permitted collection:

- Sufficient evidence **does** exist → **false refusal on an answerable item →
  Fail**.
- An independently certified negative snapshot contains **no** sufficient evidence
  → **correct abstention**, counted **only** in the negative-abstention
  denominator, **never** in positive GSR.

## 7. Closed-book vs cited-evidence

An answer that may match the reference but rests on **no permitted evidence**
(e.g. no-retrieval memorisation) **fails strict cited-evidence GSR**. Such items
are reported separately as closed-book P0 answer F1 rather than being folded
into cited-evidence GSR.

## 8. Provenance vs entailment

Record whether **provenance** — not merely semantic plausibility — was
established. A plausible answer absent from the indexed evidence is **still
unsupported** under the grounding contract. Plausibility never substitutes for a
resolvable citation.

## 9. Ambiguity handling

Multiple-valid-answer cases are handled under a **frozen alias/reference rubric**.
When gold ambiguity cannot be resolved without seeing arm labels, the item is
**quarantined and reported**. Denominators are never quietly changed after
unblinding.

## 10. Worked adjudication examples

Illustrative only (not HotpotQA gold labels). Machine-readable counterparts:
`worked_examples.jsonl`.

| # | Answer given a frozen permitted corpus | Annotation | Strict GSR |
|---|---|---|---|
| 1 | "The answer is **Paris**," with a permitted citation that directly establishes the requested city | Correct core; all claims `supported`; citation valid on all four axes | **Pass** |
| 2 | "The answer is **Paris**, which was founded in 1840," but the cited evidence supports only the city | Correct core; the founding elaboration is `unsupported` | **Fail** (core-answer diagnostic may still pass) |
| 3 | "The answer is **Paris** [source-7]," where source-7 exists but concerns an unrelated city | `exists=true`, `accessible=true`, `points_to_claim=false`, `supports_claim=false` | **Fail** |
| 4 | "I cannot determine the answer from these documents," when the permitted collection **does** contain sufficient evidence | False refusal on an answerable item | **Fail** |
| 5 | "I cannot determine the answer from these documents," when the certified negative snapshot contains **no** sufficient evidence | Correct abstention | Negative-abstention denominator **only** |
| 6 | "The answer is **Paris**," with no permitted evidence available (no-retrieval memorisation) | Answer may match reference; provenance not established | **Fail** under cited-evidence GSR; report as closed-book F1 separately |

## 11. Human review and inter-annotator gate (summary — full text in §21.3)

- **Blinding (operational definition):** reviewers see claim text, permitted
  evidence and anonymised citations in randomised order, with **no** system name,
  arm, grounder verdict, prior judge label or score. Annotate independently before
  discussing. Initial labels are locked before adjudication.
- **Bounded selection (§10.3):** audit **all** discordances when `D ≤ 50`;
  otherwise a **50-record stratified probability sample** with recorded inclusion
  probabilities. Never present a sampled review as covering all `D`. Report
  coverage `50/D` when `D > 50`; do not silently expand the staffing cap.
- **Concordant screen:** seeded random **30** concordant traces — 15 accepted in
  both arms, 15 rejected in both (or all available in a cell, with the shortfall
  disclosed). This is a **screen for shared blind spots**, not a precise estimate
  of rare hallucination rates.
- **Passes:** targeting ≥50 distinct answer-level records ⇒ `2 × 50 = 100`
  independent passes, plus adjudication. Under the bounded plan, initial passes
  are `2 × (min(D,50) + C)` with `C ≤ 30` — **at most 160** initial passes.
- **Agreement:** report raw agreement, **Cohen's κ** on the preregistered binary
  `all_material_claims_supported`, class prevalence, disagreement categories and
  adjudication rate. **κ < 0.70 triggers rubric-quality investigation and a fresh
  blinded calibration — not automatic label manipulation.** Class imbalance makes
  κ misleading, so give a prevalence-sensitive reading (optionally Gwet's AC1).
- **Shortfall:** if fewer distinct records exist, audit all and report the
  shortfall. Do not claim human-validated strict GSR if agreement stays low or no
  genuinely independent reviewers are available — report automated scores as
  **judge-dependent/provisional** instead.

## 12. Never

- Never re-label a post-repair candidate as shared upstream input.
- Never use the modified production grounder as its own sole judge; hold the
  independent rubric fixed for **both** arms.
- Never retry-and-replace an unfavourable valid judgment with a new judge.
- Never tune examples or discuss reviewer disagreements against the holdout to
  improve an A/B delta.