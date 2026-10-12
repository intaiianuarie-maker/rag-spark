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

## 2026-10-12 T-V1-010 (One documented serialization boundary in auth-client)
TASK: T-V1-010 (MICRO-TICKETS-V1 §B, trace RP-06/RP-07 · QF-06/QF-07)
CHANGE:
  - apps/web/src/lib/auth-client.ts — new module-level `serializeBody(body, headers)` + `isWireReadyBody` + case-insensitive `hasHeader`. Rules: (1) `undefined` → no body, no header; (2) wire-ready BodyInit — string, FormData/File, Blob, ArrayBuffer, ArrayBufferView (TypedArray/DataView), URLSearchParams, ReadableStream — forwarded BY REFERENCE, untouched, with NO content-type added by us (a hand-set content-type would drop FormData's runtime `boundary=…`); (3) everything else (objects/arrays/numbers/booleans/null) → `JSON.stringify(body)` ONCE + `content-type: application/json` only when the caller left it unset. `authFetch.call()` now calls it (GET still sends no body); `authPost` (login/register/logout) re-routes through the SAME function, so request-body `JSON.stringify` in this file went 2 → 1 — only serializeBody (line 185); the other remaining `JSON.stringify` (line 228) is safeFetch's synthetic 502 RESPONSE literal. Boundary documented in: a ~40-line contract block above `serializeBody`, a file-header bullet, an `AuthFetchInit.body` doc comment, and a pointer on `authFetch`.
  - apps/web/src/lib/auth-client.test.ts — +9 contract tests in `describe("serialization boundary (T-V1-010)")` (22 → 32): object-in/object-out encoded exactly once (`JSON.parse(body)` deep-equals input AND is not a string); FormData same-instance passthrough + parts intact + no content-type; ArrayBuffer/TypedArray identity + byte equality + no content-type; NO string sniffing (pre-JSON string `'{"already":"json","n":1}'` byte-identical, `'plain text'` not re-quoted, no content-type guessed); JSON-looking string nested in an object stays data; caller-supplied `Content-Type: application/merge-patch+json` respected; GET never carries a body; FormData re-sent as the SAME instance across 401 → refresh → retry (1 refresh, 2 attempts); AuthApiError still thrown on a body-carrying POST; login goes through serializeBody (once).
WHY: ticket instruction "JSON-serialize plain objects exactly once; pass FormData/binary through untouched; document the boundary" — metrics: serialization decision points N → 1, heuristic branches: 0 (dispatch is on the runtime TYPE of the body, never on the CONTENT of a value; no "does this string already look like JSON?" branch exists anywhere). Satisfies RP-06/QF-06 (single boundary that T-V1-008/T-V1-009 hand objects to) and keeps the RP-07/QF-07 binary path explicit for T-V1-012/T-V1-013.
REUSED: none (no new deps). Reused this file's existing injectable-fetcher harness (`scriptedFetcher`/`jsonResponse`/`createAuthClient`) and the existing vitest node config at apps/web/vitest.config.ts.
BUILT: `serializeBody`/`isWireReadyBody`/`hasHeader` — ~20 lines of custom type-dispatch glue, because `AuthFetchInit.body` is intentionally `unknown` and must branch on wire-readiness instead of guessing. Reusing a serializer lib did not fit: adding a dependency for one `JSON.stringify` would move the boundary, not collapse it.
VERIFY:
  - cd apps/web && npx vitest run → 6 files / 70 tests passed (auth-client 32, ask 5, search 15, trace-panel 8, status-chip 4, backend proxy route 6)
  - cd apps/web && npx tsc --noEmit → exit 0 (no output)
  - cd apps/web && npx eslint src/lib/auth-client.ts src/lib/auth-client.test.ts → exit 0
  - T-008/T-009 helpers unaffected: ask/contracts.test.ts 5 passed + search/contracts.test.ts 15 passed in the run above; plus an integration probe living OUTSIDE the repo tree (/tmp/opencode/tv1-010-probe, 2 passed) that drives the REAL singleton auth client with the REAL helpers: askQuestion → `POST /api/backend/v1/workspaces/ws-1/ask`, `JSON.parse(body)` === `buildAskBody("what is RP-06?","gpt-x")` and is not a string, content-type application/json; searchDocuments → same for `/search` with `buildSearchBody(...)`. Re-run: `cd /tmp/opencode/tv1-010-probe && "/root/code/Super Rag/knowledge-os/apps/web/node_modules/.bin/vitest" run --config ./vitest.config.mjs`
  - no sniffing / one decision: `grep -n "JSON.stringify" apps/web/src/lib/auth-client.ts` → line 185 (serializeBody) + line 228 (safeFetch synthetic 502 response literal) only; `JSON.parse` appears once, at `readJson` (RESPONSE side).
RISKS: low — wire behavior for every current caller is byte-identical (all `body:` call sites pass object literals or undefined; full suite + tsc + eslint green). Two deliberate, documented deltas: (a) a caller-supplied `content-type` is now respected instead of being clobbered by `application/json`; (b) a raw STRING body passes through verbatim instead of being quoted into JSON — no current caller passes a string, and the alternative requires exactly the string-sniffing this ticket forbids. FormData/Blob now reach the proxy untouched, but the Next proxy (`apps/web/src/app/(auth)/api/backend/[...path]/route.ts`) is still text/arrayBuffer-based — byte-identity end-to-end is T-V1-012/T-V1-013, browser e2e is T-V1-011. A one-shot `ReadableStream` body cannot be replayed across the 401 retry (documented in the contract block). No commit/deploy/restart performed. Allowed files honored: `apps/web/src/lib/auth-client.ts` + `apps/web/src/lib/auth-client.test.ts` + this handoff row; the probe lives in /tmp/opencode (outside the repo). FOREIGN paths observed during this ticket and NOT touched by me (parallel dev1/reviewers): `M` apps/api/app/authz/principal.py, apps/api/app/documents/reads.py, apps/api/app/documents/upload.py, apps/worker/jobs/ingest_worker.py, packages/answer/engine.py, packages/graph/extractor.py, tests/integration/test_ingestion_jobs.py; `??` packages/core/errors.py, tests/contracts/test_error_taxonomy_contract.py, tests/unit/test_error_taxonomy.py, handoffs/CODE1-Q0-HANDOFF.md (plus the handoffs/*.md dirt already present when I started).

## 2026-10-12 T-V1-011 (Browser Ask + Search end-to-end proof)

TASK: T-V1-011 (MICRO-TICKETS-V1 §B, trace RP-06 · QF-06; depends T-008/T-009/T-010)
CHANGE:
  - handoffs/CODE2-HANDOFF.md — this row only (evidence ticket; no product files touched).
  - Everything else lives OUTSIDE the repo tree in /tmp/opencode/tv1011/ (throwaway repro): `relay.py` (raw-TCP tee), `drive.js` (Playwright browser driver), `browser-evidence.json`, `relay.jsonl`, screenshots `01-after-login.png` / `02-ask.png` / `03-search.png`.
WHY: spec instruction "prove the transport fix through the real proxy, not just units" — T-008/T-009/T-010 only proved the helpers emit objects in unit tests; this ticket drives the REAL Chromium → REAL Next dev server (`next dev`, real `apps/web/src/app/(auth)/api/backend/[...path]/route.ts` proxy) → REAL local FastAPI (compose `knowledge-os-api-1` on 127.0.0.1:8000, bind-mounted repo code, `--reload`) and captures what the BACKEND actually received. No mock backend anywhere in the chain (reviewer checklist #3).
REUSED: Playwright 1.x + Chromium headless shell (Apache-2.0) installed into /tmp/opencode/tv1011 (outside the repo) purely as the browser driver; Python stdlib asyncio for the relay. Nothing new added to the repo; no product dependency changed.
BUILT: `relay.py` — a byte-identical raw-TCP pass-through tee (127.0.0.1:8899 → 127.0.0.1:8000) that logs every HTTP message on BOTH directions (request line, headers, raw body bytes + sha256 + JSON decode type; upstream status line) and forwards the original bytes untouched, so the real FastAPI still parses/authorizes/answers each request. Needed because uvicorn access logs carry no bodies and adding logging middleware to the API would violate "touch NO product files".
VERIFY (commands + output, verbatim):
  1. Topology (relay + real Next dev, both started by me, both stopped after capture):
     ```
     $ cd /tmp/opencode/tv1011 && python3 relay.py 127.0.0.1:8899 127.0.0.1:8000 /tmp/opencode/tv1011/relay.jsonl &
     {"ts": 1791810865.942, "event": "relay_listening", "listen": "127.0.0.1:8899", "upstream": "127.0.0.1:8000"}

     $ cd "/root/code/Super Rag/knowledge-os/apps/web" && API_BASE_URL=http://127.0.0.1:8899 npx next dev -p 3005
     ▲ Next.js 16.4.0 (Turbopack)
     - Local:         http://localhost:3005
     ✓ Ready in 2.6s
     ```
  2. Browser drive (`node drive.js`, real Chromium; env TV1011_EMAIL/TV1011_PASSWORD = a throwaway user registered against the local API, workspace "T-V1-011 Evidence B" = 2893534e-193f-4056-a3d1-f2ad598d3fb4):
     ```
     [net:login] POST http://localhost:3005/api/auth/login -> 200
     [login] status = 200 body: {"ok":true}
     [net:login] GET http://localhost:3005/api/backend/v1/workspaces -> 200
     [net:ask] POST http://localhost:3005/api/backend/v1/workspaces/2893534e-193f-4056-a3d1-f2ad598d3fb4/ask -> 200
     [ask] browser status = 200
     [net:search] POST http://localhost:3005/api/backend/v1/workspaces/2893534e-193f-4056-a3d1-f2ad598d3fb4/search -> 200
     [search] browser status = 200
     WROTE /tmp/opencode/tv1011/browser-evidence.json
     ```
     Browser-visible request bodies (Playwright `request.postData()`):
     - ask:    `{"query":"What is retrieval precision RP-06 and how is it measured?","model":"gpt-4o-mini"}`
     - search: `{"query":"retrieval precision citation validity","mode":"hybrid","filters":{"type":null,"source":null,"date":null,"collection":null,"language":null}}`
     UI render proof (screenshots): `/tmp/opencode/tv1011/02-ask.png` (assistant answer bubble rendered = proxy returned the upstream body unchanged), `/tmp/opencode/tv1011/03-search.png` ("No results. Try a broader query or fewer filters." = upstream `{"results":[],"mode":"hybrid"}` rendered).
  3. BACKEND-RECEIVED bodies (exact bytes on the socket into the real FastAPI, from relay.jsonl) + upstream statuses:
     ```
     POST /v1/workspaces/2893534e-193f-4056-a3d1-f2ad598d3fb4/ask HTTP/1.1   (authorization: Bearer <kios_access cookie token>, content-type: application/json)
       bytes 91  sha256 105934fc314d48639997721a59c40e46c0f06142624af19957fbc43617176c08
       raw: {"query":"What is retrieval precision RP-06 and how is it measured?","model":"gpt-4o-mini"}
       decoded_type: dict   python_type_is_object: True   keys: ['model','query']
     HTTP/1.1 200 OK  (content-length 432, content-type application/json, x-request-id present)   [4.60 s round trip: req 1791811558.599 → resp 1791811563.203]

     POST /v1/workspaces/2893534e-193f-4056-a3d1-f2ad598d3fb4/search HTTP/1.1 (same auth header, content-type: application/json)
       bytes 149 sha256 e86714d594be9de80393f3f7be09b3a6143128343c30b0e5060292aaae79a319
       raw: {"query":"retrieval precision citation validity","mode":"hybrid","filters":{"type":null,"source":null,"date":null,"collection":null,"language":null}}
       decoded_type: dict   python_type_is_object: True   keys: ['filters','mode','query']
     HTTP/1.1 200 OK  (content-length 30)   [0.66 s: req 1791811572.943 → resp 1791811573.604]
     ```
     Response bodies (200 both):
     - ask:    `{"answer":"I cannot answer this question based on the provided documents.","status":"insufficient_evidence","reason_code":"EVIDENCE_MISSING","reason":"Accessible sources do not contain sufficient evidence to answer.","confidence":{"level":"low","basis":"No relevant documents retrieved or model declared missing evidence","probability":null},"limitations":[],"trace_id":"1afd0193d2264e05944e2d5b0fdd6…","claims":[],"citations":[]}` (honest abstention: the throwaway workspace has no documents; the transport, not the corpus, is under test)
     - search: `{"results":[],"mode":"hybrid"}`
  4. Bodies decoded as objects accepted by the REAL request models (run INSIDE the running API container, same bind-mounted code the 200s came from; read-only import):
     ```
     $ docker exec -e ASK_RAW="…" -e SEARCH_RAW="…" knowledge-os-api-1 python -c "from app.ask.router import AskRequest; from app.search.search import SearchRequest; …"
     ask    python type : dict | model: True
     search python type : dict | model: True
     AskRequest.model_validate(ask)    -> AskRequest(query='What is retrieval precision RP-06 and how is it measured?', model='gpt-4o-mini', mode=<RetrievalMode.HYBRID: 'hybrid'>)
     SearchRequest.model_validate(search) -> SearchRequest(query='retrieval precision citation validity', mode=<RetrievalMode.HYBRID: 'hybrid'>, filters=SearchFilters(type=None, source=None, date=None, collection=None, language=None))
     ```
METRICS (spec): Ask 200 = YES (browser + backend); Search 200 = YES (browser + backend); bodies decoded as objects server-side = 2/2; serialization boundaries observed on the wire = 1 per path (no `"\"{\\\"query…\""` double-encoded string anywhere — grep the raw bodies above).
SUCCESS CRITERIA: handoff contains both captured bodies + both 200 responses — met.
REPRO (outside the repo, from a clean shell):
  ```
  cd /tmp/opencode/tv1011 && python3 relay.py 127.0.0.1:8899 127.0.0.1:8000 $PWD/relay.jsonl &
  cd "/root/code/Super Rag/knowledge-os/apps/web" && API_BASE_URL=http://127.0.0.1:8899 npx next dev -p 3005 &
  cd /tmp/opencode/tv1011 && TV1011_EMAIL=$(cat email2.txt) TV1011_PASSWORD="Tv1011-Evidence-Pw-9" node drive.js
  ```
  (Playwright + chromium already installed in /tmp/opencode/tv1011; user/workspace were created via the local API as ticket setup, then logged in through the real /login UI.)
RISKS: low — evidence ticket, zero product-file changes. Two observations for the coordinator: (1) `APP_PROXY_TIMEOUT_MS = 15_000` in the Next proxy vs measured Ask latency 8.6–16.7 s against the live OpenRouter gateway — a slow Ask can legitimately 502 ("backend_unreachable") at the proxy even though the backend answers 200; this run took 4.6 s, but the margin is thin and worth a follow-up ticket (raise timeout or make Ask async/SSE) — NOT fixed here (route.ts is out of my allowed files). (2) The Ask/Search answers are honest abstentions (`insufficient_evidence` / `results: []`) because the evidence workspace has no documents; the transport proof does not depend on corpus content, but a corpus-backed re-run is trivial by pointing the same script at a workspace with documents.
GIT SELF-REPORT (start → end, `git status --porcelain` in knowledge-os):
  - start: `M apps/api/app/adapters/infinity_probe.py, M handoffs/CODE1-HANDOFF.md, M handoffs/COORDINATOR-HANDOFF.md, M handoffs/MUSER-LOOP-STATE.md, M handoffs/REW1-REVIEW-HANDOFF.md, M handoffs/REW2-REVIEW-HANDOFF.md, M tests/integration/test_live_endpoints.py` + 13 untracked handoffs/*.md.
  - end: `M handoffs/CODE2-HANDOFF.md` (mine, this row) + `M handoffs/COORDINATOR-HANDOFF.md, M handoffs/MUSER-LOOP-STATE.md, M handoffs/REW1-REVIEW-HANDOFF.md, M handoffs/REW2-REVIEW-HANDOFF.md, M packages/ingestion/dedup.py` + the same 13 untracked handoffs/*.md.
  - My footprint = `handoffs/CODE2-HANDOFF.md` ONLY. FOREIGN churn observed and NOT touched by me (parallel dev1): `infinity_probe.py`, `CODE1-HANDOFF.md`, `tests/integration/test_live_endpoints.py` went clean, and `packages/ingestion/dedup.py` became dirty (looks like T-V1-007 in flight) — HEAD stayed `d134e64` the whole ticket. Throwaway repro, node_modules, chromium and both dev processes live outside the repo tree; relay + next dev were stopped after capture. No commit / no deploy / no service restart.
  - POST-WRITE CORRECTION (same ticket, no new file touched): HEAD moved `d134e64 → 767fc87` mid-run from the COORDINATOR's commit (my capture/evidence ran entirely on the `d134e64` tree; the transport files under test — ask/contracts.ts, search/contracts.ts, auth-client.ts, proxy route.ts — are byte-identical in 767fc87, so the evidence still applies). Also newly dirty in the end-state (NOT mine, parallel dev1 = T-V1-007): `tests/integration/test_dedup_versions.py`; my footprint remains `handoffs/CODE2-HANDOFF.md` only (75 insertions).

## 2026-10-12 T-V1-012 (Next proxy forwards raw/streaming upload bodies)

TASK: T-V1-012 (MICRO-TICKETS-V1 §B, trace RP-07 / QF-07; shares the T-V1-010 transport contract — no JSON re-serialization)
CHANGE:
  - apps/web/src/app/(auth)/api/backend/[...path]/route.ts — request bodies now cross the proxy as RAW BYTES on a stream:
    - removed the body materialization (`request.text()` for everything except multipart/octet-stream, `request.arrayBuffer()` for those two) — the UTF-8 decode→re-encode was the byte-mutation bug (RP-07);
    - new `MAX_UPLOAD_BYTES = 25 MiB` (mirrors `packages/ingestion/normalize.py` `MAX_DOCUMENT_BYTES`), exported; new exported `UploadTooLargeError` + `boundedBodyStream(source, limit)` — a TransformStream passthrough that enqueues chunks UNCHANGED while counting them; overflow does `controller.error(typed)` (never buffers past the cap, aborts the in-flight upstream upload); cancel propagates bounded→source (defined cancellation both ways);
    - advisory `Content-Length` fence (isdigit && > cap → 413 `upload_too_large`, mirroring `apps/api/app/documents/upload.py`) BEFORE any byte is read or the backend is contacted; actual-count enforcement happens mid-stream for absent/lying lengths;
    - `content-type` forwarded VERBATIM incl. multipart `boundary=…` (comment added);
    - fetch init: `{ body: boundedStream, duplex: "half" }` (typed `RequestInit & { duplex: "half" }` — DOM lib lacks `duplex`) + `AbortSignal.any([AbortSignal.timeout(15_000), request.signal])`;
    - catch: stream teardown `body.cancel()` (no dangling upload), `instanceof UploadTooLargeError` → 413, client hang-up (`request.signal.aborted`) → 499 `client_disconnected`, else the EXISTING 502 timeout/unreachable shape+messages unchanged.
  - apps/web/src/app/(auth)/api/backend/[...path]/route.test.ts — +8 tests (6 → 14): 3-stage byte identity on a hostile invalid-UTF-8 multipart fixture (arrival-time first-chunk = fixture prefix; boundary verbatim), non-multipart octet-stream byte identity through the real `POST` handler, JSON byte-for-byte (parses to the same object at the handler — no re-serialization), Content-Length fence → 413 pre-contact (fetch never called), mid-stream overflow → 413 (exactly 24 MiB forwarded, 7th chunk cut, 0 bytes stored, infinite source torn down), client cancel → 499 + upstream aborted + inbound source cancelled, `boundedBodyStream` verbatim-passthrough + typed overflow error, `boundedBodyStream` cancel→source propagation.
WHY: spec instructions (1) "replace request.text() re-encoding with bounded raw/stream body forwarding", (2) "preserve multipart content-type + boundary; enforce size limit + cancellation". Metric "byte-mutation … → identical (after)": hostile fixture is 189 B raw and the OLD path renders it 205 B (+16, mutated — see control below); AFTER: 189/189 identical at all 3 stages. T-V1-010 contract honored: the proxy neither parses nor stringifies — the bytes auth-client's single `JSON.stringify` produced reach FastAPI untouched (serialization decisions stay at 1, heuristic branches 0).
REUSED: none (no new deps). Node/web-platform primitives only: `TransformStream`/`ReadableStream` passthrough, `AbortSignal.any` (env Node v22.22.1), existing vitest node config, existing `forward()` + stubbed-fetch harness and fixtures in route.test.ts.
BUILT: `boundedBodyStream` + `UploadTooLargeError` + `uploadTooLargeResponse` (~40 lines custom glue) — the bound must hold on the ACTUAL byte count while streaming: a pre-buffer would reintroduce unbounded memory, and a content-length-only check trusts the client. An upload-size library would wrap the same TransformStream, not remove it.
VERIFY (commands + output, verbatim):
  - cd apps/web && npx vitest run → `6 files / 78 tests passed` (route.test 14 = 6 pre-existing + 8 new; auth-client 32, ask 5, search 15, trace-panel 8, status-chip 4)
  - cd apps/web && npx vitest run "src/app/(auth)/api/backend/[...path]/route.test.ts" → `1 file / 14 passed`
  - cd apps/web && npx tsc --noEmit → exit 0; npx eslint route.ts route.test.ts → exit 0
  - Byte identity through the REAL `forward()` (throwaway probe OUTSIDE the repo: /tmp/opencode/tv1012-probe, repo vitest binary, `next/headers` stubbed, real route.ts imported by absolute path):
    ```
    PRE-TRANSPORT   bytes=189  sha256=a8f215055fcb5cf2ed5b805dc1b402912fc9e1e485228fa2aa23b87bf37b719d
    AT-HANDLER      bytes=189  sha256=a8f215055fcb5cf2ed5b805dc1b402912fc9e1e485228fa2aa23b87bf37b719d  (first chunk bytes=189 sha256=a8f215055fcb5cf2ed5b805dc1b402912fc9e1e485228fa2aa23b87bf37b719d)
    STORED          bytes=189  sha256=a8f215055fcb5cf2ed5b805dc1b402912fc9e1e485228fa2aa23b87bf37b719d
    FORWARDED CT    multipart/form-data; boundary=----kios-tv1012-boundary
    JSON PRE         bytes=57  sha256=5729e99baa6beab3b199d781b55105fa89bd46d67fecae631e5ac10a36d540c8
    JSON AT-HANDLER  bytes=57  sha256=5729e99baa6beab3b199d781b55105fa89bd46d67fecae631e5ac10a36d540c8   (equal; JSON.parse round-trips to the same object)
    ```
    Re-run: `cd /tmp/opencode/tv1012-probe && "/root/code/Super Rag/knowledge-os/apps/web/node_modules/.bin/vitest" run --config ./vitest.config.mjs` → 2 passed
  - Hostility control (does the fixture actually detect the bug?): same fixture through `Buffer.from(x.toString("utf8"), "utf8")` (= the old `text()` path) → `bytes 189 → 205 (+16), sha256 eb4f38b4488d468c00d437ed607aaa30df1390594015146ae03717553b985719, mutated=true`.
  - JSON routes unaffected: full suite 78/78 incl. T-V1-010 auth-client 32, T-V1-008 ask 5, T-V1-009 search 15, and the 6 pre-existing proxy tests (GET/DELETE/204/205/304/405/auth) unchanged and green.
RISKS: medium-low — full suite + tsc + eslint green; wire behavior for JSON/GET/DELETE identical. Deliberate deltas for the reviewer: (1) 413 mid-stream is only guaranteed `instanceof`-mapped when the error identity survives the fetch boundary — under real undici an aborted upload may surface as a wrapped error and fall through to the existing 502 shape; the deterministic clean-413 path is the Content-Length fence (mirrors the backend's two-tier fence). (2) Client disconnect now answers 499 instead of 502-`backend_unreachable` (the response is unsent either way — client is gone; keeps logs honest). (3) Proxy no longer buffers bodies: when the client declares no content-length, upstream receives chunked transfer-encoding (uvicorn/h11 accepts chunked; the backend's own fence + `_read_limited` still enforce). (4) Stream bodies are one-shot — a 401→refresh→retry cannot replay a body through this proxy (T-V1-010 documented the same one-shot limitation at the auth-client layer). NOT touched: the 15 s proxy timeout vs slow Ask (T-V1-011 observation — route.ts was my allowed file but the timeout is out of this ticket's scope). No commit / no deploy / no restart. Allowed files honored: `route.ts` + `route.test.ts` + this handoff row; probe lives in /tmp/opencode (outside the repo).
GIT SELF-REPORT (start → end, `git status --porcelain` in knowledge-os):
  - start (formal capture, /tmp/opencode/tv1012-start-status.txt): `M handoffs/COORDINATOR-HANDOFF.md, M handoffs/MUSER-LOOP-STATE.md, M handoffs/REW1-REVIEW-HANDOFF.md, M handoffs/REW2-REVIEW-HANDOFF.md` + 13 untracked `handoffs/*.md`; HEAD `198b4c3`. NOTE: ticket pins repo @ `b274a17`, but the coordinator had already committed `b274a17 → 198b4c3` during discovery — `git diff b274a17 198b4c3 --stat -- route.ts route.test.ts auth-client.ts` is EMPTY (transport files byte-identical, so the ticket's pin applies to exactly this tree). Minutes earlier (still at b274a17) the worktree also showed `M handoffs/CODE1-HANDOFF.md, M packages/ingestion/dedup.py, M tests/integration/test_dedup_versions.py` — they went clean in that coordinator commit (FOREIGN churn, not mine).
  - end (final capture, after this row was appended): `M apps/web/src/app/(auth)/api/backend/[...path]/route.ts` + `M …route.test.ts` + `M handoffs/CODE2-HANDOFF.md` (all MINE — the two allowed code files and this row; handoff diff is +36/−0, purely additive) + FOREIGN, NOT touched by me: `M docker-compose.yml, M handoffs/CODE1-HANDOFF.md, M handoffs/COORDINATOR-HANDOFF.md, M handoffs/MUSER-LOOP-STATE.md, M handoffs/REW1-REVIEW-HANDOFF.md, M handoffs/REW2-REVIEW-HANDOFF.md, M packages/retrieval/adapters/pg_fts.py, M packages/retrieval/adapters/qdrant_store.py` + the same 13 untracked `handoffs/*.md` (`docker-compose.yml` + `pg_fts.py` + `qdrant_store.py` + `CODE1-HANDOFF.md` went dirty MID-ticket, after my start capture — parallel dev1, not mine). HEAD stayed `198b4c3` for the whole ticket. Zero foreign code paths touched; probe, node_modules and no dev processes were added to the repo. No commit / no deploy / no restart.
