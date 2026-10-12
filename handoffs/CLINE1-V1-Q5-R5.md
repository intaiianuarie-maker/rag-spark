# CLINE1-V1-Q5-R5/R7 Handoff — V1 Q5 Re-review 5+7 Response (P1 + P2 closed)

> Durable session handoff per standing order (AGENTS.md §4).
> Agent: Cline (Backend) · Continuation of CLINE1-V1-Q5-grounding.md.
> **Response to w4:p17 REQUEST CHANGES in `reviews/V1-Q5-REREVIEW-5.md` and
> `reviews/V1-Q5-REREVIEW-7.md`** — closes the remaining P1 and P2.

---

## TASK

1. Close P1 from `reviews/V1-Q5-REREVIEW-5.md`: indirect attribution
   (`Alice said the launch is approved.`, `According to Alice, the launch is approved.`)
   returned `score=1.0` and promoted to HIGH because the structural guard only
   rejected quoted spans and colon-introduced claims.

2. Close P2 from `reviews/V1-Q5-REREVIEW-7.md`: after the P1 fix, the
   comma-fronted attribution frame was too broad — `On Friday, the launch is
   approved.` and `In the report, the launch is approved.` also abstained.

Q9/product acceptance stays open until coordinator accepts. No merge, no
deploy, no v2 edits.

## STATUS

**P1 and P2 CLOSED — code fix + regressions in place, all green.** Holding for
independent re-review per AGENTS.md §2.6 (w4:p17 merges). Not merged.

Uncommitted working-tree changes on `feat/v2-graph`:

- `packages/answer/verification.py` — added `_is_attributed_frame()` (4 frames),
  `_REPORTING_VERB` regex, `_QUOTE_CHARS` constant, sentence-aware
  `_find_supporting_sentence()` with locate(), narrowed Frame 3 to require a
  reporting verb (P2 fix), removed `report` from the verb class, updated
  docstrings. Fixed RUF002 (EN-DASH -> HYPHEN-MINUS in docstring).
- `tests/unit/test_answer_engine.py` — +227 lines; 14 new test functions
  covering indirect attribution (negatives), comma-not-attribution (positives),
  denied/reported/qualified, decimal safety, Unicode spans, and citation
  span-to-accepted-sentence.

## CHANGE

### `packages/answer/verification.py` (uncommitted, vs `4b7ca89`)

| Area | Before | After |
|---|---|---|
| Attribution frames | None | 4-frame structural guard |
| Frame 1 (quoted) | N/A | `_QUOTE_CHARS` adjacencies |
| Frame 2 (colon) | `":" in before` | Same |
| Frame 3 (comma-fronted) | `"," in before` (any comma) | `"," in before AND _REPORTING_VERB.search(before)` — P2 fix |
| Frame 4 (indirect speech) | Not detected | `_REPORTING_VERB.search(before) or after` |
| Reporting verb class | N/A | Closed class, with `report` REMOVED (noun/verb ambiguity) |
| Sentence splitting | `[.!?]+` | `(?<![0-9])[.!?]+(?:\s+|$)` (decimal-safe) |
| Score offsets | `str.casefold()` | `re.IGNORECASE` on original (offsets correct) |
| Lint | RUF002 EN-DASH | Fixed |

**Key design decisions:**
- Frame 4 uses a **closed class of reporting verbs**, not an open-ended
  attribution-adjective denylist — the minimal lexical knowledge needed to
  distinguish "Alice *said* X" from "The launch is approved".
- Frame 3 requires **both** a comma AND a reporting verb before the comma,
  so "According to Alice, X" is caught but "On Friday, X" and
  "In the report, X" are not.
- `report` removed from `_REPORTING_VERB` because as a noun it appears in
  common non-attribution phrases ("In the report, ..."). The `according`
  entry alone covers the canonical "According to X, claim" pattern.

### `tests/unit/test_answer_engine.py` (uncommitted, +227 lines)

| Test | Coverage |
|---|---|
| `test_engine_indirect_attribution_abstains` (x3) | `Alice said...`, `According to Alice,...`, `...said Alice.` -> 0.0 / INSUFFICIENT_EVIDENCE |
| `test_engine_comma_not_attribution_is_grounded` (x2) | `On Friday, ...`, `In the report, ...` -> 1.0 / ANSWERED/HIGH |
| `test_engine_denied_reported_qualified_attribution_abstains` (x3) | `denied`, `reported`, `qualified` -> abstain |
| `test_engine_valid_decimal_not_split` | `1.25 percent` intact -> HIGH |
| `test_engine_partial_decimal_value_rejected` | `25 percent.` != `1.25 percent.` -> abstain |
| `test_engine_citation_span_to_accepted_sentence` | Span points to clean sentence, not rejected |
| `test_engine_unicode_before_match_citation_slice` | `Straße.` prefix doesn't shift span |
| `test_engine_unicode_in_match_citation_span` | `Straße` in claim keeps correct offsets |

## VERIFY

```
$ cd "/root/code/Super Rag/knowledge-os"
$ /root/ko-venv/bin/ruff check packages/answer/verification.py tests/unit/test_answer_engine.py
All checks passed!

$ /root/ko-venv/bin/python3 -m mypy packages/answer
Success: no issues found in 7 source files

$ /root/ko-venv/bin/python3 -m pytest tests/unit/test_answer_engine.py -q
57 passed

$ PYTHONPATH=evals /root/ko-venv/bin/python3 -m pytest tests/unit/test_graph_dataset_manifest.py -q
9 passed
```

### Direct probes (live repro of R5 + R7 finding tables)

| Source | Claim | Score | Engine status |
|---|---|---|---|
| `Alice said the launch is approved.` | `The launch is approved.` | 0.0 | INSUFFICIENT_EVIDENCE |
| `According to Alice, the launch is approved.` | `The launch is approved.` | 0.0 | INSUFFICIENT_EVIDENCE |
| `The launch is approved, said Alice.` | `The launch is approved.` | 0.0 | INSUFFICIENT_EVIDENCE |
| `Alice challenged the assertion: the launch is approved.` | `The launch is approved.` | 0.0 | INSUFFICIENT_EVIDENCE |
| `"The launch is approved," said Alice.` | `The launch is approved.` | 0.0 | INSUFFICIENT_EVIDENCE |
| `On Friday, the launch is approved.` | `The launch is approved.` | 1.0 | ANSWERED/HIGH |
| `In the report, the launch is approved.` | `The launch is approved.` | 1.0 | ANSWERED/HIGH |
| `The rate is 1.25 percent.` | `The rate is 1.25 percent.` | 1.0 | ANSWERED/HIGH |
| `The rate is 1.25 percent.` | `25 percent.` | 0.0 | INSUFFICIENT_EVIDENCE |
| `Straße. The launch is approved.` | `The launch is approved.` | 1.0 | ANSWERED/HIGH (span=(8,30)) |
| `Die breite Straße ist frei.` | `Straße ist frei.` | 1.0 | ANSWERED/HIGH (span=(11,26)) |

## RISKS AND BLOCKERS

### Risks
- **Lower answer-rate for attributed/provisional fact sentences** (they
  abstain). Mitigated: engine renders surviving verbatim-grounded claims;
  partial ratios yield PARTIAL/MEDIUM rather than false HIGH.
- **Reporting-verb closed class not exhaustive**: unknown verbs in
  indirect-speech frames default to grounded (HIGH) — conservative bias
  toward the verbatim-substring assertion, not a false HIGH on paraphrase.
- **`report` excluded from verb class**: "Alice reported the claim is approved"
  (no colon, no comma) is no longer caught by Frame 4. Acceptable: the colon
  check still catches "Alice reported the claim: the launch is approved."
- **Multilingual gate NOT closed** (disclosed): EN-to-RO/PT translation is
  suppressed in v1; separate release gate required.

### Blockers
- **v1 merge — blocked on w4:p17.** Only the reviewer merges to `main`.
  The complete fix is uncommitted on `feat/v2-graph`; not an ancestor of `main`.
- **Q9 product acceptance — NOT accepted.** V1-Q5 P1 and P2 are resolved, but
  V1 product completion / Q9 release gate remains **open** pending coordination.

## NEXT ACTION / UNBLOCKER

- **Next action:** w4:p17 re-reviews the uncommitted working-tree changes to
  `packages/answer/verification.py` and `tests/unit/test_answer_engine.py`
  against `reviews/V1-Q5-REREVIEW-5.md` and `reviews/V1-Q5-REREVIEW-7.md`.
- **Prerequisite:** review the `_is_attributed_frame()` 4-frame guard (especially
  the narrowed Frame 3), the `_REPORTING_VERB` closed class (with `report`
  excluded), and the 14 regression tests.
- **Unblocker:** w4:p17 approval -> commit + merge to `main`; Q9 card assignment
  unblocks the next v1 step.

Holding — no merge, no branch switch, no deploy — until w4:p17 responds.
