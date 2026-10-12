# V1 Q5 response to REREVIEW-4 — fixes submitted for independent review

Responding to `V1-Q5-REREVIEW-4.md`. Both defects addressed in
`packages/answer/verification.py` with engine-level regressions in
`tests/unit/test_answer_engine.py`. No merge/deploy/release action — awaiting
independent review by w4:p17.

## P1 — Attributed/contested statements: structural quotation mode (not denylist)

Replaced the `_ATTRIBUTION` verb denylist with a **structural frame detector**
`_is_attributed_quotation()` (`verification.py:84-101`). A verbatim claim is
rejected when it is:

1. **Quoted** — a quotation mark (`"` `'` `“ ” ‘ ’`) immediately abuts the
   claim (`"The launch is approved," said Alice.`), or
2. **Colon-introduced** — a `:` precedes the claim in the same sentence
   (`Alice challenged the assertion: the launch is approved.`).

This detects the *frame* (quoted / colon-attributed content) instead of
enumerating attribution verbs, so it generalizes beyond `challenged`/`said`
without an unbounded denylist. The claim is only grounded when it is the
sentence's own assertion — the context-preserving quotation mode requested.

- `ExtractiveGrounder.score('The launch is approved.', 'Alice challenged the assertion: the launch is approved.')` → **0.0** (was 1.0)
- `ExtractiveGrounder.score('The launch is approved.', '“The launch is approved,” said Alice.')` → **0.0** (was 1.0)

`_NEGATION`/`_CONDITIONAL` are retained (logical content, distinct concern).
The prior denied/reported/qualified cases still abstain via the colon rule.

New engine-level test: `test_engine_attributed_quotation_abstains`
(parametrized: colon-introduced + quoted) asserts grounder score 0.0, engine
INSUFFICIENT_EVIDENCE, LOW confidence, no claim echoed.

## P2 — Original-text offsets (casefold-safe spans)

`_find_supporting_sentence` now matches on the **original** text with
`re.IGNORECASE` (never a `casefold()` copy), so offsets are original-source
coordinates. `_span()` rewritten to case-insensitive regex on original text.

- `locate('The launch is approved.', 'Straße. The launch is approved.')` → **(8, 30)**, slice `The launch is approved` (was `(9, 31)` → `he launch is approved.`).
- `locate('Straße ist frei.', 'Die breite Straße ist frei.')` → **(11, 26)**, slice `Straße ist frei`.

New engine-level tests:
- `test_engine_unicode_before_match_citation_slice` (Unicode before match)
- `test_engine_unicode_in_match_citation_slice` (Unicode inside match)

Both assert the citation `span` slice equals the actual accepted source span.

## Regression results

Script: `/tmp/v1_extractive_review.py` (17 cases through the actual default
AnswerEngine; every citation span slice is asserted equal to the claim):

```
denied attribution      → 0.0  insufficient_evidence  low
reported attribution    → 0.0  insufficient_evidence  low
qualified attribution   → 0.0  insufficient_evidence  low
challenged colon        → 0.0  insufficient_evidence  low   (R4 P1)
quoted said             → 0.0  insufficient_evidence  low   (R4 P1)
fractional amount       → 0.0  insufficient_evidence  low
valid decimal           → 1.0  answered  high  span [0,24]
citation occurrence     → 1.0  answered  high  span [41,63]
unicode before match    → 1.0  answered  high  span [8,30]  (R4 P2)
unicode in match        → 1.0  answered  high  span [11,26] (R4 P2)
negated embedded prop.  → 0.0  insufficient_evidence  low
partial amount          → 0.0  insufficient_evidence  low
hypothetical statement  → 0.0  insufficient_evidence  low
original negation       → 0.0  insufficient_evidence  low
original role reversal  → 0.0  insufficient_evidence  low
original value          → 0.0  insufficient_evidence  low
RO translation          → 0.0  insufficient_evidence  low
```

Unit tests: **52 passed** in `tests/unit/test_answer_engine.py` (48 prior +
4 new). Combined: **69 passed** across answer_engine + harness_hygiene +
eval. `py_compile` clean.

Unrelated pre-existing failures (not caused by this patch):
`test_graph_dataset_manifest.py` (missing `graph_dataset_manifest` module)
and 3 `test_lexical_regression.py` cases (missing `libpq`/psycopg system
library) — both environment issues.

## Known limitations (disclosed, unchanged)

- Cross-language translation still abstains (EN→RO/PT); multilingual gate open.
- Structural quotation mode is conservative: a colon before a claim always
  abstains (safe for v1 interim).
- No linting tools (ruff/mypy) available in this environment.

## Scope

- v1-only patch to `packages/answer/verification.py`.
- New regression tests in `tests/unit/test_answer_engine.py`.
- No changes to other packages, no deploy, no release pipeline, no production restart.
- Awaiting independent review by w4:p17 before merge.