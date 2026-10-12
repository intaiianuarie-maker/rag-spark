# V1 Q5 response to REREVIEW-3 — fixes submitted for independent review

Responding to `V1-Q5-REREVIEW-3.md` (reviewed `4b7ca8975` / HEAD `3be1cdc`).
All three P1/P2 defects are addressed in `packages/answer/verification.py`
and covered by new engine-level regression tests. No merge, deploy, or release
action taken — this is a code submission for independent review by w4:p17.

## Changes

### Defect 1 (P1) — Denied/reported/qualified attribution now abstains

Added `_ATTRIBUTION` regex (`packages/answer/verification.py:75-77`) matching
denied/reported/qualified/alleged/disputed/claimed and their inflections. The
`_find_supporting_sentence` helper now checks this in addition to `_NEGATION`
and `_CONDITIONAL` (both in the finditer loop at lines 213-216 and the
final-sentence block at lines 228-230). A sentence where the verbatim claim
text is attributed, denied, or qualified by a third party no longer yields
score 1.0.

New test: `test_engine_denied_reported_qualified_attribution_abstains`
(parametrized: denied / reported / qualified) — all three assert
INSUFFICIENT_EVIDENCE, LOW confidence, no claim echoed.

### Defect 2 (P1) — Decimal values preserved during segmentation

`_SENTENCE_BOUNDARY` already uses `(?<![0-9])` to avoid splitting on a period
preceded by a digit, keeping `1.25` whole. Verified: `The rate is 1.25 percent.`
is grounded HIGH (score 1.0, span [0,24]), while `25 percent.` is rejected
(score 0.0). New tests: `test_engine_valid_decimal_not_split` (positive) and
`test_engine_partial_decimal_value_rejected` (negative).

### Defect 3 (P2) — Citation span points to the accepted sentence

`_find_supporting_sentence` returns the span of the *accepted* sentence,
carried through `verify_claims` into `Citation.span`. For
`It is false that the launch is approved. The launch is approved.` with claim
`The launch is approved.`, the span is now `[41, 63]` (the second, clean
sentence), not `[17, 40]` (the negated first sentence).

New test: `test_engine_citation_span_to_accepted_sentence` asserts the span
starts in the second sentence and the text at that span is the verbatim claim.

## Regression results

Script: `/tmp/v1_extractive_review.py` (13 cases, all through the actual
default AnswerEngine with repository fake LLM/orchestrator fixtures):

```
denied attribution      → 0.0  insufficient_evidence  low
reported attribution    → 0.0  insufficient_evidence  low
qualified attribution   → 0.0  insufficient_evidence  low
fractional amount       → 0.0  insufficient_evidence  low
valid decimal           → 1.0  answered  high  span [0,24]
citation occurrence     → 1.0  answered  high  span [41,63]
negated embedded prop.  → 0.0  insufficient_evidence  low
partial amount          → 0.0  insufficient_evidence  low
hypothetical statement  → 0.0  insufficient_evidence  low
original negation       → 0.0  insufficient_evidence  low
original role reversal  → 0.0  insufficient_evidence  low
original value          → 0.0  insufficient_evidence  low
RO translation          → 0.0  insufficient_evidence  low
```

Unit tests: **48 passed** in `tests/unit/test_answer_engine.py` (42 prior +
6 new). Broader suite: **65 passed** across
`test_answer_engine.py` + `test_harness_hygiene.py` + `test_eval.py`.
Syntax check: `py_compile` clean.

## Known limitations (disclosed, unchanged)

- Cross-language translation still abstains (EN→RO/PT). The multilingual
  release gate remains open and unmet.
- Keyword-based negation/attribution detection is the v1 interim approach.
  A future semantic (embedding) grounder can replace it behind the unchanged
  `ClaimGrounder` interface.
- No linting tools (ruff/mypy) available in this environment.

## Scope

- v1-only patch to `packages/answer/verification.py` (defect fixes).
- New regression tests in `tests/unit/test_answer_engine.py`.
- No changes to other packages, no deploy, no release pipeline trigger,
  no production service restart.
- Awaiting independent review by w4:p17 before merge.
