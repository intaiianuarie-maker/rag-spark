# V1 Q5 coordinator re-review — REQUEST CHANGES

Reviewed the latest V1 grounding patch in `packages/answer/verification.py` and
answer regressions in `tests/unit/test_answer_engine.py`.

The indirect-attribution correction now works for the reported cases:
`Alice said the launch is approved.` and `According to Alice, the launch is
approved.` both score `0.0`; the answer-engine suite has 55 passing tests.

## P2 — comma heuristic rejects ordinary factual sentences

`_is_attributed_frame()` treats any comma before the claim as attribution.
This correctly rejects `According to Alice, X`, but also rejects un-attributed
factual sentences such as:

- `On Friday, the launch is approved.`
- `In the report, the launch is approved.`

Both currently score `0.0` for the verbatim claim `The launch is approved.`
The broad positional rule risks false abstentions on common context-setting
clauses and can lower grounded-success/recall. Restrict the fronted frame check
to actual attribution syntax (for example, a recognized attribution opener and
comma), and add positive answer-engine controls for ordinary introductory
phrases while keeping the `According to Alice, ...` negative.

## Lint

`/root/ko-venv/bin/ruff check packages/answer/verification.py
tests/unit/test_answer_engine.py` reports RUF002 for an en dash in the module
docstring at line 30. Replace it with an ASCII hyphen or configure the intended
punctuation explicitly.

## Verification

- `pytest -q tests/unit/test_answer_engine.py` — **55 passed**.
- Direct score probes: both indirect-attribution examples score 0; the two
  ordinary comma-introduced factual examples also score 0 (false abstentions).
- Ruff: **fails** on RUF002 above.

The prior P1 reproduction is addressed in this snapshot, but V1 Q5 remains open
pending the comma-scope correction and clean lint. This review does not accept
V1 product completion or waive Q9/release gates.
