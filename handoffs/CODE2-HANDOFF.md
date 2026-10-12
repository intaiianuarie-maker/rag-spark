# CODE2-HANDOFF.md — living memory (update after every task)

## Done
- (init 2026-10-12 coordinator)
## Lessons learned
-
## Next
-

## 2026-10-12 round1 (G2 delete_workspace tests)
- Done: 7 test-only lifecycle tests, 123 graph+orchestrator green, seam-clean.
- Lesson: delete_workspace had zero coverage on both backends; docs G4 contract stale vs merged 46fbf67; G5 BLOCKED on real multi-hop corpus (2+5 rows only).

## 2026-10-12 round2 (D1+D2+D3 — G4 stale-contract + trace graph label + DECISIONS draft)
- Done:
  - D1 docs-only: G4-INTEGRATION-CONTRACT header/§0/§1 corrected — backend wiring APPLIED (5dcb6c1 factory graph + ask/engine HYBRID mode; 46fbf67 hydration + ingest hook), true remaining gap = UI mode selector apps/web.
  - D2 UI-only: trace-panel TRACE_KEYS += "graph" (order mirrors orchestrator dense/sparse/lexical/graph/rrf/rerank) + TRACE_LABELS graph="Graph" + five→six comment. No runtime change.
  - D3 proposal only (DECISIONS.md NOT edited — coordinator owns): §3-exception draft below.
  - Verify: graph scope 106 passed; factory+orchestrator+answer 104 passed; flag-off parity 1 passed; ruff packages/graph clean; mypy packages/graph 10 files clean; web vitest trace-panel 8 passed. G5 stays BLOCKED (no real corpus).
  - Leave uncommitted: my diff = docs/graph/G4-INTEGRATION-CONTRACT.md + apps/web trace-panel.ts only (other worktree dirt pre-existing, untouched).
- Lesson: task brief said "vs merged 46fbf67" but graph wiring (factory/ask/engine) actually landed in 5dcb6c1, hydration/hook in 46fbf67 — blamed both before writing; ruff must NOT be run on .md/.ts (246 false errors), scope to packages/graph.
- Next: coordinator to (a) accept/apply D3 exception note to DECISIONS.md, (b) dispatch UI mode selector (apps/web search/chat toggle), (c) keep G5 BLOCKED until real multi-hop corpus pinned.

### D3 PROPOSED TEXT (for coordinator to paste into DECISIONS.md — NOT applied)
> ## V2-GRAPH-PLAN §3 post-Q9 exception (2026-10-12, coordinator decision pending)
> - Exception GRANTED to land backend graph wiring on `main` despite the §3 MAY-NOT list (packages/answer/*, ingestion hook): (1) `packages/answer/factory.py` `graph=create_graph_retriever()` + docstring (5dcb6c1); (2) `AskRequest.mode`/`AnswerEngine.answer(mode)` HYBRID-default + `SearchRequest.mode` passthrough (5dcb6c1); (3) canonical hydration `packages/answer/verification.py` + `adapters/postgres_lookup.py` + `factory.build_answer_engine chunk_lookup` + `models.Citation.content`, ingest graph hook `apps/worker/jobs/ingest_pipeline.py`, reindex embeddings `scripts/reindex_all_chunks.py` (46fbf67).
> - Q9 gate evidence (V1, pre-merge): `reviews/V1-Q8-Q9-REVIEW-1.md` APPROVED — groundedness 98.88% (≥90%), citation validity 100% (==100%), gate 2 passed; re-confirmed post-hoc `handoffs/COORDINATOR-HANDOFF.md` HF-BENCH entry (groundedness 0.9888 / validity 1.0000, GATE PASS, V1-only arm).
> - Cross-stream APPROVE refs: `handoffs/G2-V1V2-REVIEW-2026-10-12.md` ✅ APPROVED (18/18 targeted, 638/638 full unit, 9/9 adversarial incl. positive control; C1/C2 fixed by runtime probe); `handoffs/REW1-FINAL-2026-10-12.md` ✅ FINAL PASS on merged HEAD 46fbf67 (47 Stream-A tests, ruff/mypy clean, no Stream-A files touched by merge).
> - Guardrails: flag-off parity holds (`test_graph_off_is_byte_identical_v1` green); ACL-before-ranking + citation-hydration enforced (graph summaries never ground); UI selector (`apps/web`) explicitly OUT of this exception — still pending; G5 eval stays BLOCKED (no labelled multi-hop corpus).

## 2026-10-12 T-V1-008 (Ask helper passes an object, not a string)
TASK: T-V1-008 (MICRO-TICKETS-V1, trace RP-06 / QF-06)
CHANGE:
  - apps/web/src/app/ask/contracts.ts — typed `AskRequest` + `buildAskBody(query, model?)`, `askQuestion` now passes that OBJECT to `auth.authFetch` (no `JSON.stringify`, no manual Content-Type header); doc comment marks auth-client as the single serialization boundary.
  - apps/web/src/app/ask/contracts.test.ts — NEW (5 tests): body `typeof === "object"` and not a String instance, path/method shape, model-omitted case, error propagation preserved.
WHY: spec instruction "pass the typed request object to authFetch; delete the JSON.stringify at the helper" — ends double serialization on Ask (QF-06). NOTE: the raw `JSON.stringify` + `headers: {"Content-Type": "application/json"}` removal had ALREADY landed in HEAD commit 7c452f3 (coordinator QF-06 fix); this ticket adds the typed body + the missing unit test on top. No string-sniffing heuristic added (reviewer checklist #1).
REUSED: none (no new deps); mirrors the existing pure-helper test pattern of apps/web/src/app/search/contracts.test.ts (T-V1-009 target) and existing vitest/node config in apps/web/vitest.config.ts.
BUILT: `AskRequest`/`buildAskBody` — small custom glue because ask/contracts.ts had an inline untyped body literal; extraction makes the emitted body directly assertable. Reuse of a shared type did not fit: authFetch's `init.body` is intentionally `unknown`.
VERIFY:
  - cd apps/web && npx vitest run  → 6 files / 55 tests passed (incl. src/app/ask/contracts.test.ts 5 passed)
  - cd apps/web && npx tsc --noEmit → clean (no output)
  - backend model accepts: /root/ko-venv/bin/python -c "…AskRequest.model_validate({'query':'what is RP-06?','model':'gpt-x'})" → parsed AskRequest {'query': 'what is RP-06?', 'model': 'gpt-x', 'mode': <RetrievalMode.HYBRID: 'hybrid'>}; no-model variant {'query':'plain query','model':None,…} also parses. (Success criterion: helper emits object, real AskRequest parses it — met.)
RISKS: low — behavior at the wire is identical (auth-client.ts:293 still does the one `JSON.stringify`); retry/401-refresh path in authFetch untouched (test asserts failures still propagate). Runtime e2e through the real proxy is NOT proven here — that is T-V1-012/T-V1-011's job. No deploy/restart/commit performed. Foreign changes observed in worktree during this ticket (dev1 backend + reviewers, NOT touched by me): packages/answer/models.py, packages/retrieval/adapters/pg_fts.py, packages/retrieval/adapters/qdrant_store.py, packages/core/errors.py (new), tests/unit/test_answer_engine.py, tests/unit/test_error_taxonomy.py, tests/unit/test_no_false_success.py, various handoffs/*.md.

## 2026-10-12 T-V1-009 (Search helper passes an object, not a string)
TASK: T-V1-009 (MICRO-TICKETS-V1, trace RP-06 / QF-06)
CHANGE:
  - apps/web/src/app/search/contracts.ts — exported typed `SearchRequest` ({query, mode, filters}) with a doc comment naming auth-client as the single object→JSON boundary; `buildSearchBody` return type narrowed to `SearchRequest`; `searchDocuments(workspaceId, body: SearchRequest)` signature typed (still passes the OBJECT straight to `auth.authFetch`, no `JSON.stringify`, no manual Content-Type header).
  - apps/web/src/app/search/contracts.test.ts — +5 tests (15 total): authFetch mocked, body `typeof === "object"` / not a String instance + JSON round-trip equals the backend SearchRequest shape, buildSearchBody emits a plain object, mode+non-empty filters forwarded as object fields, SearchResponse passthrough, error propagation preserved.
WHY: mirrors T-V1-008 for search per the ticket instruction — ends double serialization on the Search path (QF-06), serialization boundaries 2 → 1. NOTE: the raw `JSON.stringify(body)` + `headers: {"Content-Type": "application/json"}` removal at `searchDocuments` had ALREADY landed in HEAD commit 7c452f3 (coordinator QF-06 fix); this ticket adds the named typed body + the missing unit test on top, so the helper's emitted object is directly assertable. No string-sniffing heuristic added (reviewer checklist #2).
REUSED: none (no new deps); mirrors the T-V1-008 test pattern in apps/web/src/app/ask/contracts.test.ts (vi.hoisted authFetch mock) and the existing vitest/node config in apps/web/vitest.config.ts. `page.tsx` needed no change — its `buildSearchBody(...)` result is structurally identical to `SearchRequest`.
BUILT: `SearchRequest` type alias — small custom glue because searchDocuments' body param was an inline untyped object literal; extraction mirrors `AskRequest` on the ask path. Reuse of a shared type did not fit: authFetch's `init.body` is intentionally `unknown`.
VERIFY:
  - cd apps/web && npx vitest run src/app/search/contracts.test.ts → 1 file / 15 tests passed
  - cd apps/web && npx vitest run → 6 files / 60 tests passed (ask 5, search 15, auth-client 22, trace-panel 8, status-chip 4, backend proxy route 6)
  - cd apps/web && npx tsc --noEmit → clean (exit 0, no output)
  - backend model accepts (success criterion "backend search model parses it"): /root/ko-venv/bin/python → `app.search.search.SearchRequest.model_validate(json.loads(json.dumps({"query":"quarterly report","mode":"hybrid","filters":{all nulls}})))` → `query='quarterly report' mode=<RetrievalMode.HYBRID:'hybrid'> filters=SearchFilters(...)`; variant `{query:'q',mode:'semantic',filters:{type:'pdf',date:'2026-01-01..2026-03-31',language:'en'}}` → `mode=<RetrievalMode.SEMANTIC:'semantic'>` also parses. Read-only probe; no backend file touched.
RISKS: low — wire behavior unchanged (auth-client.ts:293 still does the one `JSON.stringify`); retry/401-refresh path in authFetch untouched (test asserts failures still propagate). Runtime e2e through the real proxy is NOT proven here — that is T-V1-011's job. No commit/deploy/restart performed. Allowed files honored: only apps/web/src/app/search/contracts.ts + contracts.test.ts and this handoff. FOREIGN changes observed during this ticket and NOT touched by me (dev1/reviewers, parallel): `packages/answer/verification.py` newly modified; untracked `packages/core/errors.py`, `tests/unit/test_error_taxonomy.py`, `tests/unit/test_no_false_success.py`, `tests/contracts/test_store_write_verification.py`, `handoffs/CODE1-Q1-HANDOFF.md`, `HF-BENCH*/HF-BENCHMARK*`, `JEV-ESCALATIONS.md`, `PROGRESS-REPORT.md`, `REPORTER-HANDOFF.md`, `REVIEW1/2-HANDOFF.md`, `REW1-FINAL-*.md`, `REW2-V4-B-R1.md`; modified `handoffs/COORDINATOR-HANDOFF.md`, `MUSER-LOOP-STATE.md`, `REW1-REVIEW-HANDOFF.md`, `REW2-REVIEW-HANDOFF.md`. Churn seen mid-ticket (start → end of my run): `apps/api/app/authz/principal.py`, `apps/api/app/documents/{reads,upload}.py`, `apps/worker/jobs/ingest_worker.py`, `packages/answer/{engine,models}.py`, `packages/graph/extractor.py`, `tests/integration/test_ingestion_jobs.py` were dirty when I started and CLEAN when I finished with HEAD still at e92a2ab — i.e. dev1 restored/re-wrote them in parallel; I did not touch them.
