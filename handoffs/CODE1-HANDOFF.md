# CODE1-HANDOFF.md — living memory (update after every task)

## Done
- (init 2026-10-12 coordinator)
## Lessons learned
- 
## Next
- review1 re-review of F2/F3/F5/F6; coordinator owns F1 (infinity recreate) + F4 (:4000 topology).

## 2026-10-12 round3 — ITERATION 1 / V1 stabilization gate (verification only, no code changes)
TASK: V1-STAB-R3 (museR directive 2026-10-12: sequential v1→v2→v3→v4, coordinator = sole merge/deploy authority) — re-run the V1 quality gate over code1's round1+round2 scope before coordinator merge. No new features, no commit, no deploy, no compose restart.
CHANGE: none (read-only round). Tree left exactly as found: 20 modified + 15 untracked files, including my 18 round1/round2 files and other agents' dirty files (trace-panel.ts, G4-INTegration-CONTRACT.md, REW1/REW2/COORDINATOR handoffs). A 35th untracked file, `handoffs/MUSER-LOOP-STATE.md`, appeared mid-run — created by another agent, untouched by me.
WHY: iteration 1 is stabilization, so the deliverable is evidence that the round1/round2 V1 fixes still hold on a shared, concurrently-edited tree before the coordinator merges. Satisfies V1-COMPLETION-PLAN §5 release gates (no fabricated vectors/citations, no false-ready) and AGENTS "never weaken a test" — nothing was relaxed to make numbers green.
REUSED: none new — existing toolchain only: `/root/ko-venv` (Python 3.14.4, pytest 9.1.1, ruff 0.16.10, mypy 2.4.0), Makefile `test-unit`/`lint`/`typecheck` targets, existing marker contract (`unit`/`integration`/`contract`).
BUILT: nothing this round.
VERIFY (exact commands + counts):
- `git status --short` → 35 lines (20 `M`, 15 `??`), no `D`; `git log --oneline -3` → 46fbf67 / dd9ce11 / 2328dfd, branch `main`.
- `pytest -q tests/unit` → **679 passed, 0 failed** (1 StarletteDeprecationWarning).
- `pytest -q tests/contracts` → **97 passed, 0 failed** (incl. 31 infinity contract tests).
- `pytest -q -m unit` → **488 passed, 374 deselected** (862 collected total); `pytest -q -m contract` → **123 passed**; `pytest --collect-only -q tests/integration` → 86 collected (not executed: needs live compose stack, restart forbidden).
- `ruff check .` → **FAIL, 9 errors**, all in files I do not own and that are unmodified vs HEAD: `tests/unit/test_ingest_pipeline.py:110` (E501), `tests/unit/test_reindex.py:3` (I001, F401), `tests/unit/test_workflow_langflow_adapter.py` (6× RUF059/RUF100/I001).
- `ruff check <my 18 touched files>` → **All checks passed** (exit 0).
- `mypy` (project gate: `files = packages, apps/api/app, apps/worker/worker, apps/worker/jobs`) → **Success: no issues found in 149 source files**.
- `mypy <my 18 touched files>` → **12 errors in 2 files**, every one pre-existing at HEAD (`tests/unit/test_answer_engine.py` 796/798/982/999 = `span` Optional-tuple index + `orchestrator.last_kwargs` stub attr; `tests/contracts/test_infinity_embedding_contract.py` 220/222/258/260/293/294/431/433 = `host, port = stub.server_address` unpack + bytes f-string). Verified against `git show HEAD:<file>` — my hunks are `test_answer_engine.py` +9 lines @150 and `test_infinity…` 1-line @175 + 33 lines @353, none overlapping those lines.
RISKS:
- Repo-wide lint is **not** clean: 9 ruff errors sit in 3 test files owned elsewhere (test1 / workflow stream). `make lint` fails until they are fixed; not mine to touch, so left as-is and reported.
- `tests/` is outside `[tool.mypy].files`, so real type debt in tests only surfaces when files are passed explicitly — the "green mypy" covers 149 source files, not tests. Worth a follow-up ticket to bring the two test files under the gate or fix the 12 findings.
- Integration suite (86 tests, incl. my `test_delete_surfaces_partial_purge_failure_as_207`) was not executed this round — needs the compose stack; last verified collect-only green. Coordinator should run it as part of merge/deploy.
- Tree is shared and moving: another agent added `handoffs/MUSER-LOOP-STATE.md` during this run; counts are a snapshot of the tree as of this entry.
Lesson: `pytest -m unit` (488) and `pytest tests/unit` (679) are different sets — 191 unit-dir tests are unmarked and 26 contract-marked tests live outside `tests/contracts`. Always state which selector produced the number, or round-over-round comparisons are meaningless.
- Left uncommitted on main; did not touch coordinator-owned F1 (infinity recreate) / F4 (:4000 topology) or other agents' dirty files.
- Next suggested Q-task: **TASK-Q0 (READY) — Establish contracts & error taxonomy** (ErrorTaxonomy, reason_code, AnswerStatus/JobStatus enums across API/worker/schemas/TS). It is upstream of the remaining Q1 "false success" surface (reason codes are how partial/degraded gets disclosed), and it is the only READY card that does not depend on coordinator-owned F1/F4. TASK-Q1 stays IN-PROGRESS (strict-dimension half landed in round2 F5).

## 2026-10-12 round2 (review1 REQUEST-CHANGES F2,F3,F5,F6 — code1)
- Done:
  - F2 purge visibility: `delete.py` returns 204 only when every store in the per-store report purged; any failure → 207 + `{status,degraded,stores,failed_stores}` body (tombstone still commits, reads 404). `dedup.py` lazy-registration `except: pass` → logged warning + fts/qdrant marked False. New integration test `test_delete_surfaces_partial_purge_failure_as_207` (live client, failing purger → 207 + report + 404).
  - F3 /ready: added non-fatal `infinity` (:7997) probe — new `adapters/infinity_probe.py` (any HTTP response = reachable), `core/config.py` +`infinity_url`, wired into `readiness.py` concurrent map. LLM gateway explicitly BLOCKED in `readiness.py` docstring (LITELLM_URL is external SaaS or coordinator-owned :4000 topology = finding F4; no cheap contractual health endpoint). Updated exact-set degraded contract (`m004_ready_degraded.py` → 5 deps, DEGRADED_CHECKS_OK) + `test_boot.py`/`test_live_endpoints.py` DEPENDENCIES.
  - F5 strict dimensions: `ALLOW_DIMENSION_ADAPTATION` default true→false (mismatch raises; explicit opt-in still pads/truncates ≥64-d). `ingest_pipeline.py` `ensure_collection(dimension=1024)` → `embed_provider.dimension`. Contract tests: `wrong_dim_512` stub mode + `test_dimension_mismatch_rejected_by_default_strict` + `test_dimension_adaptation_requires_explicit_opt_in`.
  - F6 citation content: `verify_claims` now sets `Citation.content` to the exact canonical content grounding ran against (retrieved item text, or store-hydrated canonical for graph path); `_citation()` helper too; store-resolved citations already carried it. Field kept (populated, not reverted). New test `test_verify_claims_citation_carries_canonical_content`.
- Verify: unit 679 passed; contracts 97 passed (incl. 31 infinity, +2 new); ruff clean; mypy clean (8 files); m004 degraded script DEGRADED_CHECKS_OK; live curl :8000/health ok + :8000/ready shows infinity reachable (GET / → 307). Integration `test_dedup_versions.py` collect-only OK (9 tests; live run needs compose stack, skips here).
- Lesson: exact-set /ready contracts (`m004` fixture) turn a 1-line probe addition into a 4-file change — keep the dep set pinned in exactly those 3 places (fixture, test_boot, test_live_endpoints). Infinity root returns 307, so the probe must treat any HTTP response as reachable, not just 200.
- Left uncommitted on main; did not touch coordinator-owned F1/F4 or other agents' dirty files.
- Next: F1 infinity recreate + F4 :4000 topology remain coordinator-owned; LLM-gateway /ready probe unblocked only after F4 topology settles.

## 2026-10-12 round1 (Q9-prep classify_query)
- Done: REAL query classifier in ask/router.py, 678 unit pass, ruff+mypy clean.
- Lesson: full lingua dep overkill for routing hint; heuristic + disclosed limits wins; per-workspace routing_rules needs Alembic migration decision.

## 2026-10-12 T-V1-001 — Correct the lookup-citation docstring
TASK: T-V1-001
CHANGE: packages/answer/verification.py (module docstring lines 14-16 only, 3 lines rewritten, 0 logic lines)
WHY: RP-03, QF-03 — docstring must describe the store-grounding branch (lines 507-521) so future edits don't regress it
REUSED: none
BUILT: docstring-only rewrite (no reuse fit; no custom logic)
VERIFY: `grep -n 'cannot be grounded' packages/answer/verification.py` → empty (exit 1); `/root/ko-venv/bin/ruff check packages/answer/` → All checks passed
RISKS: none — docs only; no behavior, perf, or security surface; left uncommitted, no deploy/restart

## 2026-10-12 T-V1-002 — Close the legacy None-content citation path
TASK: T-V1-002
CHANGE: packages/answer/models.py (Citation.content field comment, 61-70, docs only); tests/unit/test_answer_engine.py (appended test_citation_content_guarantee_is_grounder_gated)
WHY: chose option (b) — softened the F6 guarantee to the grounder-gated paths of verify_claims, with claim_grounder=None store-lookup named as the sole documented None case (RP-03, QF-03); option (a) (drop falsy-content citations at verification.py:522-524) was rejected because it breaks the pinned legacy contract in the existing, append-only test_verify_claims_store_lookup_scoped_positive (lookup Citation without content must still verify under existence+scope only)
REUSED: none (no new dependency; pydantic BaseModel field stays unchanged)
BUILT: comment rewrite + one appended test pinning both halves: source text names grounder-gated/claim_grounder=None, legacy no-grounder lookup still verifies with content=None, grounder-gated lookup without canonical content drops the citation
VERIFY: /root/ko-venv/bin/pytest tests/unit/test_answer_engine.py -q → 70 passed (49 tests at HEAD → 50, +1 new); /root/ko-venv/bin/ruff check packages/answer/models.py tests/unit/test_answer_engine.py → All checks passed; git diff --stat shows only models.py + test file for this ticket (verification.py untouched, 0 logic lines changed)
RISKS: docs-only on the guarantee — no behavior change, so no ACL/citation/perf surface; residual: legacy no-grounder path can still emit content=None (documented, engine wires ExtractiveGrounder by default so production is grounder-gated); left uncommitted, no deploy/restart

## 2026-10-12 T-V1-003 — Remove the dead `_citation()` helper
TASK: T-V1-003
CHANGE: packages/answer/verification.py (deleted dead `_citation()` def, old lines 220-237 + 1 trailing blank line, -20 lines, 0 logic lines added); handoffs/CODE1-HANDOFF.md (this row)
WHY: RP-03 — chose option (a) delete, not (b) route-through: `_citation()` had zero call sites repo-wide (grep -rn "_citation(" packages/ → empty), and routing the retrieved non-graph path through it would have to split the shared graph/non-graph `Citation(...)` append at 483-499 (the helper reads provenance off `item` directly and cannot accept the graph-hydration overrides doc_id/filename/page/section/content_to_verify), i.e. real churn on the grounder hot path to resurrect dead code. Delete is grounder-path behavior change: none by construction — the diff removes only the uncalled def; `Citation` and `_span` are still used by the live path (484, 490) and `RetrievedItem` by the signature (401), so no import churn.
REUSED: none (no new dependency; pure deletion)
BUILT: nothing custom — removed the orphaned helper; the live inline `Citation(...)` construction stays the single citation-building site
VERIFY: `grep -rn "_citation(" packages/ | grep -v test` → empty (exit 1, dead definitions: 0); `git diff packages/answer/verification.py` → 1 file changed, 20 deletions, 0 insertions, hunk confined to old 220-237; `/root/ko-venv/bin/ruff check packages/answer/` → All checks passed; `/root/ko-venv/bin/mypy packages/answer` → Success: no issues found in 9 source files; `/root/ko-venv/bin/pytest tests/unit/test_answer_engine.py -q` → 70 passed (unchanged from T-V1-002 baseline)
RISKS: none measurable — deletion of uncalled code, zero grounder-path behavior diff; residual: tree is shared and pre-dirty from concurrent agents (apps/api, apps/worker, packages/answer/engine.py, packages/graph/extractor.py, several handoffs, tests/integration), none touched by me; left uncommitted, no deploy/restart
