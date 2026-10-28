# Review of Muse's proposed next work — 2026-10-17

Verdict: **agree with the V1/V2 priority and draft-PR hold; revise the dispatch before implementation.** This reviews the supplied feedback against HEAD `c46927c85119a915fa575369371a37b7c35d31ad`, the current working tree, the controlling plans, and the two October 17 reviews. It is a plan review, not acceptance or a new implementation.

## What is accurate

- The controlling development loop prioritizes V1 acceptance for dev1 and V2 for dev2. V3/V4 feature development waits for both acceptances and coordinator dispatch. Independent V2 fixtures, lifecycle corrections and evaluation preparation can proceed while G4 integration waits for live Q9.
- The displayed V1/V2/V3/V4 percentages match `handoffs/PROGRESS-REPORT.md`. They are reported task progress, not measured product quality. The Q table still lists Q1 IN-PROGRESS and the other ten READY; milestone DONE does not establish release acceptance.
- The T-V1-036 report measured three oracle-context metrics. It explicitly did not establish factual precision or Recall@20, nor live retrieval quality. Its 0.9888 result must not be promoted to an end-to-end result.
- Default process-local agent stores, double queue claims and stale workspace search responses remain confirmed defects. Shared persistence, independently started consumers and all-path UI invalidation are appropriate requirements.
- Freeze a genuine labelled corpus, measure the live retrieval path, preserve ACL-before-ranking, keep graph mode off pending evidence, and retain the existing agents/workflows work.

## Corrections to the feedback

| Feedback | Current evidence | Correct action |
|---|---|---|
| Branch is 141 commits ahead | After the earlier review publication, HEAD is **142 ahead**, zero behind freshly fetched `origin/main`. | Record immutable SHAs; counts change with documentation commits. |
| October 17 report has 2 P1 + 2 P2 | The first report has **3 P1 + 2 P2**: failing quality gates are the third P1. The follow-up adds **3 P1**. | Track all eight findings, with exposure and version scope. |
| 1d: Compose has none of the workflow DB paths | `docker-compose.yml:130–133` already requires all four paths; both services mount `workflowdata:/app/.workflow`. | Fix `.env.example`/setup consistency and test missing-store responses; do not duplicate existing wiring. |
| 1d: lexical search has no tombstone/version join | `packages/retrieval/adapters/pg_fts.py:74–97` already defines the joins and lifecycle predicate, applied before ranking. | Re-run lifecycle regressions on isolated PostgreSQL. Keep failed-purge reconciliation, but do not describe the old omission as current. |
| 2a: T-V1-036 delete error precedence still needs implementation | Commit **`6227b8d`** is an ancestor of HEAD. `_refuse_tombstoned_document_before_load` checks before loading originals and rechecks on an ingestion error. `test_delete_fence_precedence.py` covers deleted originals versus live-document storage loss. | Mark implemented, pending current release verification. The later T-V1-081 cutover record reports the crash rerun **8/8**; that is historical reported evidence, not a new run by this reviewer. |
| All factual/recall tooling is absent | The old acceptance record lacked these measurements. New H1/H2 runners exist; H2 computes **title-level Recall@10**, and H1 GSR guards empty propositions. | Inventory and extend existing runners. Recall@10 by title does not satisfy canonical EvidenceRecall@20; H1 safeguards do not prove every Q9 citation path is fixed. |
| Commit or revert all active benchmark changes to get clean | Benchmark reports, `h1_corpus.py` and several run directories are still changing independently. | Preserve them. Have their owner reconcile provenance/costs before a scoped commit, or use a clean isolated checkout for acceptance. Never revert merely to make status clean. |
| “infinity/OpenRouter” names the live embedding path | `.env.example` sets `google/gemini-embedding-2`, 3072 dimensions, with an empty Infinity URL. Older plan text names another embedding family/dimension. | Establish the actual configured provider/model/revision/dimension from runtime identity before testing; do not silently switch models or mix incompatible vectors. |

Merge/deploy authority remains coordinator-only. Repository instructions still name `w4:p17`, whereas the feedback names `museR/w4:p19`. This review does not resolve pane identity or dispatch work; it preserves the draft PR and routes the written recommendation to the user.

## Missing blockers and implementation detail

1. **False workflow verification:** `ProductionNodeExecutor` currently sets `verified=True` from a configuration flag after generation, without Stage 11/12 execution. Contain this immediately: unavailable/unverified output or a genuinely enforced disabled path. Full shared verification integration belongs in the authorized workflow repair scope. A disabled flag is only a containment measure after route/worker enforcement is tested; its name in an environment file proves nothing.
2. **Graph publication after delete:** the actual graph worker can restore tombstoned rows after extraction. Carry a lifecycle/generation fence through publication and coordinate it with deletion. A pre-write check followed by an unrelated store write still races.
3. **Graph source contribution loss:** shared entity IDs are overwritten by each document's payload; deleting the last ingested document removes evidence still supported by an earlier document. Persist contributions by workspace/document/version/chunk, update aggregates, and remove only the deleted contribution. A union of lists alone is insufficient.
4. **CI and setup:** backend, frontend and model-origin checks are failed on GitHub at the reviewed HEAD. Restore Ruff, mypy, vendor boundaries, approved-model configuration, web lint/build and complete test execution. Preserve policy intent. Distinguish liveness, dependency readiness and enabled-feature readiness.

The queue fix should acquire a SQLite write transaction **before selection**, install one unique claim without replacing an existing lease, and use token-conditioned mutations with checked row counts for ack/renew/park/recovery. The ready row has no token yet: “DELETE WHERE token” applies to lease ownership, not blindly to the ready table. Test expired-worker acknowledgements against a newer lease. At-least-once recovery still requires idempotent steps; exclusive claims alone do not guarantee exactly-once side effects.

Persisting runs, steps and queue does not persist evidence. Durable evidence and checkpoint references must be verified before V3 acceptance. For V1, reject unavailable agent/workflow execution rather than expanding a paused feature into a new subsystem. Immediate fixes are bounded correctness/containment work, not permission to start V3/V4 features.

## Revised dispatch recommendation

| Workstream | Next scoped work | Required evidence |
|---|---|---|
| dev1 / backend | Durable admission and queue ownership; default configuration; classify current eval gaps | Real API admission → separate worker process → restart recovery; simultaneous two-process claim; stale-token ack/renew/recovery; enabled-feature unavailable response |
| dev2 / frontend and V2 | Workspace request invalidation; graph contribution/lifecycle repair and fixtures | Delayed A response after switch to B, including rejection/finally; two-document entity deletion in both orders; paused graph job resumed after delete/supersession |
| Coordinator | Scope false-verification containment; review fixes; reconcile CI and benchmark artifacts | No verified result without actual evidence checks; immutable review SHA; required checks green; owner-reviewed artifact manifest |

Then finish Q0/Q1/Q3/Q4/Q5 with Q2 tracing available early enough to diagnose them, Q6 evidence UI and Q7 failure injection; prepare Q8 fixtures concurrently; run live Q9; tune only on development data in Q10. Keep the existing queue/task identifiers. Do not delay corpus labels and minimal trace propagation until every product change is complete.

The proposed 0.5 day / 2–3 days / 1–2 weeks are planning hypotheses, not estimates supported by the current evidence. Re-estimate after regression tests, corpus size, environment isolation and implementation ownership are settled. A 50–100-question pilot cannot establish tight 98% precision or per-slice 95% recall with high confidence. Publish denominators and confidence intervals, then expand the acceptance set according to uncertainty. Zero observed citation failures is an empirical result, not a population guarantee.

## Verification and limits

Re-read source and history, fetched main, and inspected PR checks. No product code changed, no services restarted, no paid model evaluation run, and no new full test run claimed. The follow-up review's **239 passing targeted tests and three offline defect reproductions** remain supporting evidence; the original **3,096 collected** count remains collection only. The original live crash suite and current lexical lifecycle behavior still require release-environment verification.

See [the follow-up findings](2026-10-17-super-rag-followup.md), [the initial review](2026-10-17-super-rag-review.md), and [the upstream-informed improvement plan](../docs/KNOWLEDGE-OS-IMPROVEMENT-PLAN-2026-10-17.md).

TASK: Review Muse's Knowledge OS next-work feedback.
CHANGE: Evidence-based corrections, missing blockers and revised dispatch recommendation.
WHY: Avoid rebuilding fixes already present and prevent oracle or historical results from being treated as live acceptance.
REUSED: Existing code, history, tests, review artifacts and controlling plans.
BUILT: Review documentation only.
VERIFY: Source/history and GitHub check inspection described above.
RISKS: Open acceptance blockers; active benchmark changes preserved; coordinator owns acceptance and merge/deploy.
