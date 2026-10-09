# CLINE1-V1-Q5 Handoff — Super RAG v1 Claim Grounding (RE-REVIEW RESPONSE)

> Durable session handoff per standing order (AGENTS.md §4).
> Agent: Cline (Backend) · Continuation of CLINE1-V1.md (v1 hardening).
> **Response to w4:p17 REQUEST CHANGES on `00579eb`** — see
> `reviews/V1-Q5-REREVIEW-2.md` and `reviews/V1-Q5-CODE-REVIEW.md`.

---

## TASK

Close Q5 (claim grounding + honest confidence) from
`reviews/2026-10-09-super-rag-review/v1-rereview.md`. `verify_claims` checked
that a cited chunk exists and is in-scope, but **not** that it supports the
claim. A fabricated answer citing a real in-scope chunk could be scored
ANSWERED/HIGH. This increment makes "cited" imply "substantiated".

## STATUS

**Two rounds of REQUEST CHANGES received, both addressed, holding for
re-review. NOT merged.** Per AGENTS.md §2.6 only w4:p17 merges / marks tasks
DONE. Both fix commits live on `feat/v2-graph` only.

```
4b7ca89 fix(verification): sentence-aware grounding (word-boundary + negation/conditional guard)
00579eb fix(answer): conservative extractive grounding; contradiction + multilingual regressions
f49303e feat(answer): semantic claim grounding + honest confidence (v1 Q5)     [reviewed → REQUEST CHANGES]
```

- **Round 1 (f49303e → `00579eb`):** reviewer found token-overlap accepts
  contradictory claims as HIGH (negation-strip, role-swap, quantity) and rejects
  valid EN→RO/PT translations. Addressed by replacing `LexicalGrounder`
  (token-overlap recall) with `ExtractiveGrounder` (verbatim-substring).
- **Round 2 (00579eb → `4b7ca89`):** reviewer showed bare substring still grants
  HIGH when a claim clause appears verbatim inside a *negated/conditional/
  partial-numeric* sentence ("It is false that the launch is approved.",
  "If the launch is approved, notify Alice.", "budget 1900 dollars" → "900
  dollars"). Substring occurrence is not factual support. Addressed by making
  `ExtractiveGrounder` sentence-aware with word boundaries + a sentence-context
  guard.

## CHANGE (commits `00579eb` + `4b7ca89`)

| Commit | File | Change |
|---|---|---|
| 00579eb | `packages/answer/verification.py` | `LexicalGrounder`→`ExtractiveGrounder` (verbatim-substring, thr 1.0); drop `_token_set`/`re`. |
| 00579eb | `packages/answer/engine.py`,`factory.py` | default + docstrings → `ExtractiveGrounder`. |
| 00579eb | `tests/integration/test_acceptance_probes.py` | probe #1 claim → verbatim chunk extract; probe #5 RO claim → verbatim substring; **new probe #6** (EN→RO/PT/mistranslation abstain). |
| 00579eb | `tests/unit/test_answer_engine.py` | 3 P1 contradiction rejects + 3 multilingual abstentions + engine negation abstention. |
| **4b7ca89** | `packages/answer/verification.py` | **Sentence-aware `score`**: split chunk into sentences; match claim with regex word boundaries (`\b900 dollars\b` ≠ "1900 dollars"); skip any sentence carrying negation/conditional markers (`not/no/false/if/unless/whether/...`); accept only a clean verifying sentence. |
| **4b7ca89** | `tests/unit/test_answer_engine.py` | **New regressions the round-2 review demanded**: `test_verify_claims_abstains_on_contextual_unsupported` + `test_engine_abstains_on_contextual_unsupported` (negation / conditional / partial-numeric, each `∈[insufficient_evidence]/[CITATION_INVALID]/[low]/0 claims`, false claim not echoed) and `test_engine_grounds_exact_value_not_partial_numeric` (precision control: "100 dollars" in a clean sentence still → HIGH). |

Diffstat `00579eb..4b7ca89`: 2 files, +145/-22. v2 `packages/graph/*`,
`.env.example`, `docker-compose.yml`, `packages/retrieval/orchestrator.py` were
**not** modified by this round (and were not in `00579eb`); their pre-existing
working-tree state is left untouched (out of v1 scope / owned by code2).

## WHY THIS ADDRESSES BOTH REVIEW ROUNDS

- **Round-1 P1 (negation-strip / role-swap / quantity):** verbatim
  word-bounded match rejects these (the contradicting clause is not a contiguous
  bounded copy of the claim) — verified by the live repro below.
- **Round-2 P1 (clause-within-sentence negation / conditional / partial-numeric):**
  handled by the sentence-context guard and word boundaries. "It is false that
  the launch is approved." contains "the launch is approved" verbatim, but the
  sentence carries the marker `false` → abstain. "If the launch is approved…"
  carries `if` → abstain. "900 dollars" vs "1900 dollars" — `\b900\b` has no word
  boundary before the `9` (preceded by `1`) → no match → abstain.
- **P2 (multilingual):** unchanged — EN→RO/PT abstains (not a substring of the
  source), which is the correct conservative behaviour; disclosed as a known,
  **unmet** v1 limitation. The multilingual gate is **not waived** and **not**
  claimed as passed.

## DESIGN NOTES / TRADE-OFFS

- **Sentence-aware extractive, not semantic, for v1**: no embedding
  cross-encoder confirmed in env; v1 stays deterministic + offline-testable. A
  future semantic grounder swaps behind the unchanged `ClaimGrounder` Protocol.
- **Marker lists are conservative by design**: any sentence containing a
  negation/conditional token is not trusted for the claim. This may over-abstain
  on legitimate conditional fact sentences ("The service may log events."); that
  is the intended conservative interim trade versus a false HIGH, per the
  review's "abstain when support cannot be established".
- **Honest-by-default**: default-on gate; model that fabricates an id + a
  verbatim-looking claim still fails unless the claim is a verbatim,
  non-negated, non-conditional, word-bounded sentence fragment of a retrieved
  chunk.

## VERIFY

```
$ cd "/root/code/Super Rag/knowledge-os"
$ /root/ko-venv/bin/python3 -m ruff check packages/answer tests/unit/test_answer_engine.py tests/integration/test_acceptance_probes.py
$ /root/ko-venv/bin/python3 -m mypy packages/answer
$ /root/ko-venv/bin/python3 -m pytest tests/unit/test_answer_engine.py tests/integration/test_acceptance_probes.py tests/integration/test_ask_route.py -q
52 passed
```

- ruff: clean. mypy `packages/answer`: 7 source files, 0 issues.
- Reviewer-set (3 files): **52 passed, 0 skipped** in this env (reviewer's 33
  passed/4 skipped = 37 collected + 8 new in round 1 + 7 new in round 2 = 52;
  the 4 ask-route skips were DB-reachability-gated and run here via TestClient
  fakes). Broader surface (incl. lexical_regression, orchestrator): **79 passed**.
- **Live repro of every reviewer row through the real engine
  (`ExtractiveGrounder`):**
  - Round-1: "The launch is approved." vs "The launch is **not** approved." →
    `insufficient_evidence`/`low`/0 (also: "It is false that…" / "If the
    launch is approved…" / "…1900 dollars." / "900 dollars" → all
    `insufficient_evidence`/`low`/0).
  - Precision control: verbatim "100 dollars" in a clean sentence →
    `answered`/`high`/1 (word-boundary guard is not blunt).
  - Multilingual: EN "The capital of France is Paris." + RO/PT/mistranslation
    claims → `insufficient_evidence` (correctly abstains; not a false HIGH).

## RISKS

- **Lower answer-rate for paraphrastic/conditional fact sentences** (they
  abstain). Mitigated: engine still renders any surviving verbatim-grounded
  claim; partial ratios yield PARTIAL/MEDIUM rather than false HIGH.
- **Multilingual gate NOT closed** (disclosed): EN↔RO/PT translation is
  suppressed in v1; separate card required. Not waived, not claimed as passed.
- **Substring/sentence brittleness**: the model's claim must be a verbatim,
  bounded, non-negated/non-conditional sentence fragment of a retrieved chunk —
  acceptable conservative v1 interim stance.

## BLOCKERS

- **v1 merge — blocked on w4:p17.** Per §2.6 only the reviewer merges to `main`.
  `4b7ca89` (parent `00579eb`+`f49303e`) is on `feat/v2-graph`; not an ancestor
  of `main`. Code-complete + green, but integration-blocked on re-review
  approval.
- **Multilingual release gate — NOT met** (disclosed; see RISKS).
- **Task selection — blocked on next READY card** (TASKS.md not in this
  worktree; outstanding v1 items deferred per CLINE1-V1.md are not v1-blocking).
  Awaiting w4:p17 assignment.
- Q5 and v1 release acceptance remain **not accepted / not shippable** pending
  re-review. v2 blocked on code2 (inter-agent ownership).

## NEXT ACTION / UNBLOCKER

- **Next action:** w4:p17 re-reviews commit `4b7ca89` (parent `00579eb`, root
  `f49303e`) on `feat/v2-graph`.
- **Prerequisite:** review the `verification.py` `ExtractiveGrounder.score`
  rewrite (diff above) + the new `test_*_contextual_unsupported` / precision
  regressions in `tests/unit/test_answer_engine.py`.
- **Unblocker:** w4:p17 — approval → merge `4b7ca89`+`00579eb`+`f49303e` to
  `main`; next READY card assignment unblocks the next v1 step.

Holding — no merge, no branch switch, no v2 edits — until w4:p17 responds.
