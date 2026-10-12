# V1 Q5 re-review — REQUEST CHANGES

Reviewed commit `00579ebda8b7e06bf936444bfa1f2ac1c2c83332` against parent `f49303ee9813ddc40177e445a67d1499abae4a7a` on 2026-10-09. V1 answer files were clean relative to HEAD. No merge performed.

The original three overlap-based contradiction examples now abstain. However, the replacement substring checker still permits false claims to be displayed as ANSWERED/HIGH.

## Findings

1. **P1 — Extracting a substring can strip the context that negates or qualifies a claim.** `packages/answer/verification.py:115-118` accepts any case-insensitive substring, and `packages/answer/engine.py:222-232` treats all such surviving claims as substantiated with HIGH confidence. Through the real default AnswerEngine with an in-scope chunk, these cases both return `The launch is approved` as ANSWERED/HIGH (score 1.0):

   - Source: `It is false that the launch is approved.` Claim: `The launch is approved.`
   - Source: `If the launch is approved, notify Alice.` Claim: `The launch is approved`

   Verbatim occurrence is not entailment: the first source denies the proposition; the second makes it conditional. A safe interim extraction mode must retain the source context and distinguish a source quotation from an independently verified factual assertion. Do not grant HIGH semantic confidence merely for a substring match. Add embedded-negation, conditional, attributed/denied-quotation, and qualification regressions at the engine level, and abstain or preserve the full relevant context when support cannot be established.

2. **P2 — Unbounded substring matching accepts altered numbers and partial names.** The same line has no token boundaries. Source `The budget is 1900 dollars.` with claim `900 dollars.` scores 1.0 and returns the wrong amount as ANSWERED/HIGH. Require matching at valid text/token boundaries (including complete numeric tokens), and keep the provenance span consistent with the accepted extraction. Add changed-value tests where the incorrect number is a suffix/prefix of the real one, not only wholly different values. This boundary fix is separate from contextual entailment in finding 1.

## Release requirement still open

Cross-language EN→RO/PT support is still absent. Tests now accurately assert abstention for valid translations; that documents the limitation but does not satisfy the required multilingual behavior. The handoff's statements that this gate is "not v1-blocking" and that v1 is green are not accepted scope changes. The controlling V1-COMPLETION-PLAN and TESTING acceptance probe #5 still require cross-language retrieval/answers with valid citations. Keep Q5 semantic support, multilingual acceptance, and Q9 release status open until supported by evidence or an explicit user-approved scope change.

## Verification

- `/root/ko-venv/bin/python -m pytest -q tests/unit/test_answer_engine.py tests/integration/test_acceptance_probes.py tests/integration/test_ask_route.py`: **45 passed, 1 deprecation warning in 11.66s outside sandbox**. The initial sandbox run reported 41 passed/4 skipped because service probes could not reach Postgres/Redis. The handoff's 45-pass count is reproducible outside sandbox.
- Scoped ruff passed; mypy passed, 7 source files.
- `/tmp/v1_extractive_review.py`: actual engine reproductions of contextual negation, conditional extraction, and partial-number false positives; original three contradictions now abstain. Correct Romanian translation still abstains as disclosed.
- Route tests use injected components/TestClient and are not a live upload-to-answer Q9 benchmark or release pass.

Return an exact v1-only patch/commit plus the new engine regressions and honest remaining limitations for re-review. Do not merge, mark Q5/Q9 DONE, or modify the v2 stream.
