# CODE1-Q0-HANDOFF — TASK-Q0 error taxonomy

> **⚠️ TREE-STATE WARNING FOR THE COORDINATOR (museR).** This sub-agent's
> uncommitted changes were wiped from the working tree **five times** between
> ~14:20–14:58 CEST by concurrent activity (tracked files reverted via
> checkout-class operations after commits `8599a52`, `e92a2ab`, `23dbb5e`;
> untracked files deleted at least twice — consistent with the
> `MUSER-LOOP-STATE.md` "~14:15 UTC reverted N runaway paths" operations and
> the fresh-dev1 slice resets). Every restore below is byte-identical to what
> was verified. **Durable copies + idempotent re-apply script live OUTSIDE the
> repo:** `/tmp/opencode/q0_files/{errors.py,test_error_taxonomy.py,
> test_error_taxonomy_contract.py}` and `/tmp/opencode/q0_reapply.py`
> (re-run it after copying the three files back — it skips already-applied
> edits and fails loudly on unexpected states). If any file listed under
> CHANGE is missing/reverted in the tree you are reading, restore from those
> copies before merging. Verification counts below were measured at ~14:53
> CEST on exactly this content; all 18 Q0 tests + ruff were re-run green on
> the restored tree at 14:59.

TASK: TASK-Q0 — Establish contracts & error taxonomy (V1-COMPLETION-PLAN §3;
TASKS.md "Explicit error taxonomy, reason codes, status enum across
API/worker". V1 stabilization only, no new features).

CHANGE: (one line per file; all uncommitted on `main`)
- `packages/core/errors.py` (NEW, 652 lines) — canonical taxonomy:
  `ErrorCategory` (answer/ingestion/indexing/storage/api/auth), `ReasonCode`
  StrEnum (44 UPPER_SNAKE codes: 5 answer + 14 ingestion/pipeline + 3 indexing
  + 4 store-lifecycle incl. the 5 parallel-task codes STORE_WRITE_UNVERIFIED,
  EMBED_UNAVAILABLE, ZERO_VECTOR_REJECTED, INDEX_COUNT_MISMATCH,
  PARTIAL_PURGE + 18 legacy-pinned API codes), `ErrorSpec` registry
  (category, http_status, retryable, safe user message, documented `wire`
  value), `LEGACY_ALIASES` (wire/lowercase → canonical), helpers
  `canonical_code` / `normalize_code` / `error_detail` / `spec_for`
  (fail-open on unknown codes).
- `packages/answer/engine.py` — the 5 reason_code literals now come from
  `ReasonCode.<X>.value` (wire values identical).
- `packages/answer/models.py` — docstring on `Answer.reason_code` (type stays
  `str | None`: wire-compatible). NOTE: the tree also carries another
  agent's uncommitted `Citation.content` docstring hunk — untouched by me.
- `packages/graph/extractor.py` — docstrings now point at
  `core.errors.ReasonCode` as the canonical registry (was "Stream A owns Q0").
- `apps/worker/jobs/ingest_worker.py` — `record_failure` persists
  `normalize_code(exc.code)` (DB + API job-poll speak UPPER_SNAKE);
  `internal_error` literal → `ReasonCode.INTERNAL_ERROR.value`.
- `apps/api/app/documents/upload.py` — envelope built via `error_detail(...)`;
  `DOCUMENT_TOO_LARGE / EMPTY_DOCUMENT / JOB_NOT_FOUND / QUEUE_UNAVAILABLE /
  INVALID_CHARSET` from the registry; job-status `error_code` normalized on
  read (legacy lowercase DB rows included).
- `apps/api/app/documents/reads.py` — `_http_error` + the 3 literal 404
  envelopes via `error_detail`; `_job_error` code fallback normalized.
- `apps/api/app/authz/principal.py` — `invalid_token` / `auth_unavailable`
  envelopes via `error_detail` (wire unchanged).
- Other `apps/web/**` files — **NONE touched**. Only
  `apps/web/src/app/ask/contracts.ts`: purely additive exports `AnswerStatus`
  and `AnswerReasonCode` unions; no existing line changed.
- `tests/unit/test_error_taxonomy.py` (NEW, 13 tests) — registry invariants:
  completeness, UPPER_SNAKE, uniqueness, http-status rules, safe-message
  lint (no hostnames/IPs/SQL/tracebacks/format strings), legacy-alias
  resolution, frontend-pinned wire preservation, fail-open, envelope shape,
  IngestionError subclass coverage.
- `tests/contracts/test_error_taxonomy_contract.py` (NEW, 5 tests) — TS
  mirror sync (contracts.ts unions ↔ Python), live API envelope shape via
  `create_app()`+TestClient (401 without auth, `{"detail":{"error","message"}}`,
  registered wire value), regex-extraction of every `IngestPipelineError("…")`
  literal in `ingest_pipeline.py` + every `code = "…"` in
  `ingestion/normalize.py` resolving through the registry, and
  `ReasonCode.*` references in `ingest_worker.py` being real members.
- `tests/integration/test_ingestion_jobs.py` — 3 asserted literals updated to
  the normalized wire values (reportable test edits, values not weakened):
  L214 `"unsupported_mime_type"`→`"UNSUPPORTED_MIME_TYPE"`,
  L234 `"parse_failed"`→`"PARSE_FAILED"`,
  L252 `detail.error "document_too_large"`→`"DOCUMENT_TOO_LARGE"`.

WHY: Decision = **one canonical registry, deliberate split-case wire policy**.
UPPER_SNAKE_CASE is the canonical code case because
`packages/graph/extractor.py` pins it as the shared Q0 contract; worker/
ingestion codes are therefore uppercased on the wire (job.error_code in DB,
job-poll `error_code`, upload envelopes) — safe because a full-repo grep
(including `apps/web`) found **no frontend literal** for any of them, only
Python test literals which were updated. API auth/workspace/document codes
keep their lowercase wire values because `apps/web/src/lib/auth-client.ts`,
`keys-client.tsx`, `members-client.tsx`, `switcher.tsx`, `login-form.tsx`,
`refresh/route.ts` AND ~20 integration-test assertions pin them — registered
instead as canonical UPPER entries with the pinned value documented as
`ErrorSpec.wire` + `LEGACY_ALIASES`. This satisfies the TASKS.md Q0 row
("Explicit error taxonomy, reason codes, status enum across API/worker") and
V1-COMPLETION-PLAN §3 Q0 ("ErrorTaxonomy, reason_code, AnswerStatus/JobStatus
enums across API/worker/schemas/TS contracts"): AnswerStatus/JobStatus Python
enums already existed (unchanged, no migrations); TS mirror added additively
for the ask surface. DECISIONS.md has no numbered row for Q0; the governing
rows are the "One-framework / adapter-boundary" rule (taxonomy is core glue,
stdlib-only, no vendor code) and D1/D2-style wire-freeze precedents — wire
values only changed where nothing pinned them, exactly as the Q0 task card
prescribes.

REUSED: none — no new third-party code. stdlib `enum.StrEnum` +
`dataclasses` only; FastAPI's existing `HTTPException(detail=…)` envelope
reused as the wire shape (no new middleware).

BUILT: custom `packages/core/errors.py` (~40 registry entries + resolution
helpers) because reuse did not fit: the repo had 3 ad-hoc error shapes
(`ApiError`/`AuthorizationError`, `AuthErrorBody`, `WorkspaceErrorBody`) and
3 code-cases (UPPER in answer engine, lowercase in worker/API) with no shared
registry; a full framework (e.g. problem-details) would have changed the
frozen `{"detail":{"error","message"}}` contract and broken the frontend.

VERIFY: (all at ~14:53 CEST, restored tree re-confirmed at 14:59)
- `/root/ko-venv/bin/pytest -q tests/unit` → **695 passed, 0 failed**
  (baseline of the concurrently-moving tree was 679→706 during the session as
  other agents' slices landed/were reverted; includes my 14 new unit tests).
- `/root/ko-venv/bin/pytest -q tests/contracts` → **102 passed, 0 failed**
  (97 pre-existing + my 5 new contract tests).
- `pytest -q tests/unit/test_error_taxonomy.py
  tests/contracts/test_error_taxonomy_contract.py` → **18 passed** (re-run
  on the restored tree, 14:59).
- `/root/ko-venv/bin/ruff check` on every touched file (11 files) →
  **All checks passed**.
- `/root/ko-venv/bin/mypy packages/core/errors.py packages/answer/engine.py
  packages/answer/models.py packages/graph/extractor.py
  apps/api/app/documents/upload.py apps/api/app/documents/reads.py
  apps/api/app/authz/principal.py` → **Success: no issues in 7 source files**.
- `/root/ko-venv/bin/mypy` (project gate) → **RED, NOT from Q0**: 2×
  `attr-defined` in `apps/worker/jobs/ingest_pipeline.py` (L269/L385
  `count_ids` missing on `PostgresLexicalStore`/`QdrantVectorStore`), from
  the parallel QF-10 reindex work; `ingest_pipeline.py` + `packages/retrieval`
  are on my hard NEVER-EDIT list and were untouched (verified via
  `git diff HEAD`). `ingest_worker.py` cannot be mypy-isolated because it
  imports `jobs.ingest_pipeline`; its own lines are error-free.
- Integration suite: not executed (needs live compose; restart/curl
  forbidden). The 3 updated literals are listed under CHANGE.

RISKS:
- **Wire-compat**: ingestion codes uppercased on the wire
  (`error_code` in job poll, upload envelopes). No frontend consumer found;
  downstream consumers that string-matched lowercase (external scripts,
  other agents' pending tests) will see `DOCUMENT_TOO_LARGE` etc. Legacy
  lowercase INPUT still resolves (`normalize_code`), so old DB rows read
  back canonical.
- **Legacy split-case is deliberate but semi-permanent**: 18 API codes keep
  lowercase wire values pinned by `apps/web` + tests; migrating them needs a
  versioned contract + coordinated frontend change (Recommended follow-up #1).
- **Concurrency/wipe risk (realized 5×)**: see TREE-STATE WARNING. Anyone
  reverting the working tree re-loses this task; recover from
  `/tmp/opencode/q0_files/` + `/tmp/opencode/q0_reapply.py`.
- **Registry additions by parallel streams**: `STORE_WRITE_UNVERIFIED`,
  `EMBED_UNAVAILABLE`, `ZERO_VECTOR_REJECTED`, `INDEX_COUNT_MISMATCH`,
  `PARTIAL_PURGE` are registered per the parallel task's codes as instructed;
  if that task changed its final code names, adjust the registry entries
  (aliases keep inputs working either way).
- `apps/api/app/documents/delete.py` and `adapters/readiness.py` (Dev1
  round-2, restricted) were NOT touched; `delete.py`'s passthrough codes
  (`document_not_found`, `store_purge_failed`) are registered as legacy-wire
  entries so a later `error_detail()` adoption there is drop-in.
- Project mypy gate currently red from QF-10 work (see VERIFY) — coordinator
  should not attribute that to Q0.

Recommended cross-file follow-up:
1. Coordinator-owned, versioned: migrate the 18 legacy lowercase API wire
   values to canonical UPPER (registry already holds both), with a single
   frontend + integration-test sweep; until then `ErrorSpec.wire` documents
   each pinned value.
2. `apps/api/app/documents/delete.py` (restricted file): adopt
   `error_detail(exc.code, exc.message)` — behavior-identical.
3. `apps/worker/jobs/ingest_pipeline.py` (forbidden): raise
   `ReasonCode.X.value` instead of lowercase literals at source; until then
   the worker normalization boundary handles it.
4. `apps/api/app/auth/{errors.py,schemas.py}` + `auth/workspaces/schemas.py`
   ad-hoc bodies (`validation_error` etc.): route through `error_detail`;
   codes are already registered.
5. `apps/web` (code2): a documents/search TS mirror of `JobStatus`/job
   `error_code` codes could extend the additive ask-contracts block; Q0 kept
   its TS edit strictly to `ask/contracts.ts`.
6. `packages/agents` `AgentErrorCode` (v3/v4 run-state enum) is a separate
   subsystem — not folded into Q0; fold in deliberately if/when the agent
   surface ships.
7. Fix the QF-10 mypy break (`count_ids` attributes) so the project gate
   returns to green — owner: the reindex/QF-10 stream.
