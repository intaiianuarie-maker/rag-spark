# PREREGISTRATION — RAG-Spark benchmark v1 (H2-FULLDEV pilot)

**Status: FROZEN except judge rubric (see below). Dated 2026-10-15, before any
holdout scoring. Amend only with a dated reason before unblinding; after
results, label exploratory and require a new untouched test set.**

| Field | Frozen contract |
|---|---|
| Protocol | `H2-FULLDEV` with corpus manifest SHA-256 (below), scratch ACL scope |
| Corpus | HotpotQA distractor validation, 7,405 questions; dedup rule: normalized (lowercased trimmed title + whitespace-collapsed paragraph); unique 66,635 / collisions 7,065 / titles 66,581 |
| Corpus SHA-256 | `cb67996071b5b515389702dd77eb42d563a50ccf80c515bcd7e92be213467fd3` |
| Primary scored data | 150 held-out HotpotQA validation IDs, 75 bridge / 75 comparison, seed 42 |
| Sample SHA-256 | `b05f7a1398f730722a853b7f98ffd06115d4868faf3adeb6f8c2f4f5d2247ede` |
| Primary estimator | Strict binary GSR per (question, arm, repeat); per-question mean over exactly R=2 frozen repeats; equal bridge/comparison weight |
| Primary contrast | `Δ = GSR(T-G01 ON) − GSR(T-G01 OFF)` |
| Primary uncertainty | 10,000-resample stratified question-clustered paired bootstrap, fixed RNG seed, arms/repeats resampled together |
| Primary success claim | Positive point estimate AND two-sided 95% CI entirely above 0, plus all predeclared safety/quality/cost gates |
| Missing/failures | Timeout, provider error, rejected answer, invalid citations, unsupported claims → GSR=0 for that attempt; audit every failure |
| Sample size rule | Exactly R=2, N=150, precommitted before scoring; report `NOT EXECUTED/INCOMPLETE` if blocked |
| McNemar | Optional, on frozen Repeat 1 binary pairs only |
| Budget | $2.00 billed-service hard ceiling including preflight; no Phase 3 authorization. AMENDED 2026-10-15 (pre-unblinding): one-time H2 index build fund ~$1.60 authorized (66,635 vectors, reusable scratch artifact, per-call ledgered, abort on overrun). Phase 2 H1 runs inside $2.00. |
| Contamination | Mandatory 50-item P0 no-context diagnostic before any public H2 capability/competitive claim |
| T-G01 branch | Post-generation only (commit `6f03fac`); shared-upstream A/B valid; flag `AGENT_PARAPHRASE_GROUNDER` default OFF |
| Experiment A (replay) | DEFERRED — TRACE-GAP recorded T-P01: no replayable captured traces exist; relabelling unrelated arms forbidden |

**NOT FROZEN: judge rubric.** `judges/rubric_v1.md` (`e3929b11…`) is
PROPOSAL_ONLY. Phase 2 scoring cannot start until the rubric + real
development-set calibration examples freeze via dated amendment below.

## Amendments

| Date | Change | Reason |
|---|---|---|
| 2026-10-15 | Budget: +~$1.60 one-time H2 index fund; Phase 2 = H1 now, H2 after index | User decision at Phase 0 exit (P02 BLOCKED-UNPRICED); before unblinding |
