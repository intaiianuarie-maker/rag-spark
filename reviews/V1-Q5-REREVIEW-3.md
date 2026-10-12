# V1 Q5 re-review 3 — REQUEST CHANGES

Reviewed `4b7ca8975d091f2b984aaff49490055f5d3bb54a` (HEAD `3be1cdc`, a subsequent handoff-doc commit) on 2026-10-09. V1 answer/test files have no uncommitted changes. Nothing merged.

The previously reported examples now abstain. Three defects remain in the sentence/marker approach:

1. **P1 — Denied/attributed text is still promoted to factual HIGH-confidence output.** `packages/answer/verification.py:64-72,165-170` considers the absence of a finite marker list sufficient for support. Source `Alice denied the claim: the launch is approved.` with claim `The launch is approved.` returns ANSWERED/HIGH, score 1.0. The source reports a denial, not approval. This is the same unresolved entailment problem, not a request to add one more keyword. For the interim mode, preserve complete relevant source context as an explicitly attributed quotation rather than asserting an arbitrary extracted clause as verified fact; otherwise use support verification that can establish the claim and abstain when it cannot. Do not justify HIGH semantic confidence using lexical matches. Add denied/reported/qualified quotation regressions through the engine.

2. **P1 — Splitting on every period corrupts decimal values and defeats the numeric boundary fix.** `_SENTENCE_BOUNDARY` at verification.py:56 splits `The rate is 1.25 percent.` into fragments. Claim `25 percent.` then matches a fresh fragment boundary and is returned as ANSWERED/HIGH, even though the rate is 1.25. Conversely, the correct complete claim `The rate is 1.25 percent.` is rejected. Preserve decimal/numeric tokens during segmentation and validate boundaries against the original source offsets. Add decimal/fractional-value positive and changed-value negative engine tests.

3. **P2 — The citation span can point to an occurrence the grounder rejected.** `_span` at verification.py:75-80 always finds the first occurrence, independently of the supporting sentence accepted by `score`. For source `It is false that the launch is approved. The launch is approved.` and claim `The launch is approved.`, scoring skips the negated first sentence and accepts the second, but the resulting citation span is `(17,40)` inside the negated sentence. This violates exact evidence location. Carry the accepted source span through grounding into citation construction rather than performing an independent first-match search. Test repeated claim text where the first occurrence is qualified/denied and a later occurrence is accepted.

All three reproduced through the actual default AnswerEngine using repository fake LLM/orchestrator fixtures. Script: `/tmp/v1_extractive_review.py`.

## Verification

- Reviewer three-suite selection: **52 passed, 1 unrelated deprecation warning in 10.60s**, outside sandbox, with no skips.
- Scoped ruff passed; mypy passed (7 source files).
- Prior contradiction and numeric-suffix examples now abstain as expected.
- Valid EN→RO translation still abstains. Multilingual support remains an unmet requirement, as disclosed; Q5 semantic acceptance and Q9 release acceptance remain open.
- Passing TestClient/fake-component probes do not establish a live upload-to-answer benchmark or release pass.

Return a v1-only patch and meaningful regression results. Prefer a coherent limited quotation mode or actual support verification over incremental keyword additions. Do not merge or mark release gates complete.
