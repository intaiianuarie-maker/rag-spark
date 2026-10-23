# Super RAG project review — OpenCode, 2026-10-15

Independent review of the whole project (plan, implementation, live state), taken
after the 2026-10-15 morning review and the same-day I-wave fixes. Read-only: no
product code changes, no restarts, no deployments, no messages to other agents,
no paid model calls.

**Snapshot discipline:** the tree is live (multi-agent). Full backend suite ran
at `b5e957c`; source-level checks re-verified after the tree advanced to
`94c2747` (T-I01c landed mid-review). Every claim below states which snapshot it
used.

## Verdict

v1 is **not yet release-acceptance-ready** — consistent with the project's own
"live Q9 BLOCKED" status and the 2026-10-14/15 reviews. The I-wave response was
fast and largely effective, and the full test suite now collects and runs for
the first time (T-I05 verified). But: 3 of 5 review findings are not fully closed
in production topology, this week's fixes regressed the now-unblocked suite in
measurable places, the static gates are red tree-wide, the live deployment is
stale, and measured answer quality sits far below the Q9 release gates.

## 1. What holds up (freshly verified)

- **Full backend suite unblocked (T-I05).** First whole-suite evidence ever:
  1873 collected, **1861 passed, 12 failed, 1 skipped** in 6m35s in `/root/ko-venv`
  (`pytest -q -m 'unit or contract'`, at `b5e957c`). The exact repro from the
  morning review now collects clean.
- **Frontend green (re-run):** 225/225 vitest, `tsc --noEmit` clean.
- **Backing-service exposure fixed** (the 2026-10-09 P0): Postgres 5432, Redis
  6379, Qdrant 6333/6334, SeaweedFS S3 8333 all bound `127.0.0.1` (`ss` +
  compose port mappings); ufw default DROP.
- **T-I02 (compose workflow stores): FIXED.** Four `WORKFLOW_*_DB` fail-closed `:?`
  bindings in the shared backend-env anchor (`docker-compose.yml:130-133`),
  `workflowdata:` named volume mounted in both api and worker. Empirically
  verified with `docker compose config`: vars resolve for both services; with
  exports missing, compose refuses to create anything with a clear error
  (fail-closed, restart-bomb gone). Var names match the required-path checks in
  `store.py:26-36/97-103` and `router.py:766-773` exactly.
- **T-I03 (lexical lifecycle gate): FIXED.** The FTS query LEFT JOINs
  `documents` + `document_versions` and gates `deleted_at` / `current_version_id` /
  `superseded_at` in WHERE ahead of ranking (`pg_fts.py:73-93`, `:218-231`),
  mirroring the dense store's authority (`qdrant_store.py:345-416`).
  Added tests are genuine: behavioral proofs against the **real Postgres**
  (retained tombstone row not returned, superseded row not returned, live row
  still returned) plus structural SQL guards (predicate before ORDER BY/LIMIT,
  UUID cast text-safe) — `tests/unit/test_pg_fts_lifecycle_gate.py:159/193/216/286/295/304/316`.
  Failed FTS purges are retained rows now filtered at query time — the exact
  case the fix closes.
- **T-I04 (web workspace scoping):** committed `888873c`; 225-test suite incl.
  the scoping cases passes on re-run.
- **T-I06b (sensitive-workspace egress refusal on the write path):** the ingest
  pipeline loads workspace policy and raises *before* any embed call
  (`apps/worker/jobs/ingest_pipeline.py:966-971`). Verified in source. (But see
  finding F2 — it introduced a taxonomy regression.)
- **T-I07b (lost-page disclosure): FIXED in code, but with no regression
  guard.** `chunk_disclosure` merged into both FTS and Qdrant index metadata
  (`ingest_pipeline.py:902`, `:1025`, identity merged after disclosure per
  T-R06), retrieved metadata passes through verbatim (`pg_fts.py:249`,
  `qdrant_store.py:1088`), hydration retains it (`hydration.py:288-301`), and
  the answer engine converts `parse_lost_regions` into limitation text +
  `NEEDS_REVIEW` relabel + confidence cap on **every** return path
  (`engine.py:511-590`); `/search` also surfaces it as item metadata
  (`search.py:272`). Gap: `chunk_disclosure` appears in **no** test file — the
  existing disclosure tests inject synthetic metadata and prove only the
  answer layer. **Reverting lines 902/1025 would fail no test.** The fix's own
  verification was regression-green only (45 ingest tests) — add a
  write-path assertion (pipeline record metadata carries `parse_lost_regions`
  into both index writes).
- **T-I01c wiring landed mid-review** (after the morning review's correct
  "seam-only" complaint): both entrypoints install the shared store when
  `AGENT_STORE_DB` is set (`apps/api/app/main.py:61-65`,
  `apps/worker/worker/main.py:49-53`), and `tests/unit/test_t_i01c_xproc.py` is a
  real cross-process proof (separate subprocesses, distinct PIDs, shared queue
  depth visible) — the R11-style test the review asked for.
- **Eval wave produces real measurements** (`evals/v1_full_results.json`):
  evidence_recall@20 100%, factual_precision 100%, honest survivorship-bias
  note, metered spend, per-strata Wilson intervals. The regression diagnosis
  (verbatim grounder + unweighted faithfulness = 79.2% of failures) is a
  plausible, falsifiable hypothesis.

## 2. Blocking findings (P1)

### F1 — Durable agent queue is opt-in and volume-less; the original P1 behavior persists by default

- `AGENT_STORE_DB: ${AGENT_STORE_DB:-}` — empty default means the shipped
  topology still boots with split in-memory queues/stores
  (`docker-compose.yml:134-137`). The morning review required "refuse admission
  when that production wiring is unavailable"; nothing refuses.
- **No shared volume backs the knob.** api and worker mount only `graphdata` and
  `workflowdata` (`docker-compose.yml:222-225`, `:243-245`). Setting
  `AGENT_STORE_DB` to any ordinary path gives each container a *private* file —
  the same bug class the project already fixed twice (T-R11 graph, T-I02
  workflow) and documented extensively in its own comments.
- **Latent defects in the new SQLite stores, live-relevant once wired:**
  1. `SqliteAgentRunQueue.claim` is a non-transactional SELECT→DELETE→INSERT
     (`packages/stores/agent_stores.py:567-588`): two processes can both pass the
     SELECT; the loser's no-op DELETE is unchecked yet it still returns a Claim →
     **double execution**. The `threading.Lock` only guards one process.
  2. No WAL mode / busy_timeout on any of the per-op
     `sqlite3.connect(self.path)` calls → `database is locked` risk under
     api+worker contention.
  3. Lease drift: SQLite claim hardcodes `ls = 30.0` (`:570`) while the
     in-memory queue honors `AGENT_QUEUE_LEASE_SECONDS` (default 120s,
     `packages/agents/queue.py:72-81`); the worker never renews mid-run.
  4. The seam switches run/step/checkpoint/queue but **not the evidence store**
     (`:488-513`) — worker-recorded evidence stays invisible to the API even
     when wired.
  5. `SqliteAgentRunQueue` does not subclass the `AgentRunQueue` ABC.
- The xproc test covers enqueue→depth visibility only; there is no
  cross-process claim/execution test (the exact double-claim surface above).

### F2 — The I-wave fixes regressed the full suite (the "per-file green" failure mode repeated, same day)

12 failures at `b5e957c`; at least 5 are direct fallout of this week's accepted
fixes — all committed with focused-suite-only evidence:

- `POLICY_BLOCKED` is raised as a raw string (`ingest_pipeline.py:968`) but was
  never added to the `ReasonCode` taxonomy (`packages/core/errors.py`) →
  `test_error_taxonomy_contract` and `test_graph_queue_reliability` fail.
- The ask factory now passes `sensitive=`, breaking the pinned
  `test_local_only_no_egress` factory double (`TypeError` in
  `apps/api/app/ask/router.py:251` path).
- `tests/unit/test_openrouter_provider.py` reads `api_key` from `os.environ`
  (came with T-ARCH-02) → the harness-hygiene contract test fails.
- The 2026-10-15 triage note itself said the A-wave's "suites green" claims were
  "per-file, not whole-suite." The same verification gap reproduced in the I-wave
  the same day. **Fix is cheap and binding now that collection works (~7 min
  run): make the full suite + ruff + mypy a DONE gate.**

### F3 — Static gates are red tree-wide

- **mypy cannot run at all**: duplicate module `rerank` (`packages/rerank/` vs
  `apps/rerank/`) aborts with 1 error before checking anything. The TESTING.md
  CI gate is unmeetable as-is, and every recent "mypy clean" handoff claim holds
  only per-file.
- **ruff: 27 errors** — 24 in the *uncommitted* `evals/bfcl_*`/`test_bfcl_*`
  files, 4 line-length violations in committed `tests/unit/*` files.

### F4 — The live deployment is stale

`GET /version` on the running API: `git_sha: "unknown"`, workflow
`store_modes` all `in-memory`, `draft_migration: "pending"`. The running
containers predate the I-wave: **none of this week's reviewed fixes are live**,
and the build carries no provenance (the R03 finding from 2026-10-14, still
open). Deployment is coordinator-gated (standing "no restarts" respected), but
v1 acceptance is defined on the live path — the gap must be closed deliberately:
export the new required vars (`WORKFLOW_*_DB`, `AGENT_STORE_DB`, plus existing
fail-closed `AUTH_JWT_SECRET`, `VECTOR_COLLECTION`) and redeploy the current
tree, then verify `/version` and `store_modes` reflect it.

### F5 — Q9 quality gates remain far away (now measured, honestly)

First full-N run (`eval-31712164ff0a`, 48 items): grounded_answer_rate **20.8%**
(gate ≥90%), citation_validity **45.8%** (gate 100%); recall/precision 100% PASS
(survivorship-bias caveat named by the team itself). The corpus is a single
multi-hop stratum — no EN/RO/PT, table, or unanswerable slices yet (Q8), so
`correct_abstention` is vacuous (0 unanswerable items). The T-G01
(paraphrase-tolerant grounder) A/B is the right next experiment, with one
caution: paraphrase tolerance must not weaken the no-hallucination guarantee —
prefer improving claim→citation binding at generation time over loosening
verification, and add an adversarial hallucination fixture to the A/B so the
gate can catch traded-off safety.

## 3. Lower-severity findings (P2)

1. **API :8000 and web :3001 are internet-reachable** — verified `200` on the
   external IP (`173.249.55.242`). Docker's DNAT bypasses ufw (DOCKER-USER chain
   is empty; compose maps `"${API_PORT:-8000}:8000"` /
   `"${WEB_PORT:-3001}:3000"` without `127.0.0.1` binding). The T-037
   justification comments say "host-local tooling only" — that is factually not
   true under current exposure; `/health`, `/ready`, `/version` are
   unauthenticated and public. Bind loopback or front via the existing nginx TLS.
2. **Kanban board :8095 is ufw-ALLOWed worldwide** (deliberate rule) — internal
   project data (ticket states, plans) is exposed publicly. Restrict to
   tailscale/loopback.
3. **Acceptance probes are dual-marked `unit`+`integration`**
   (`tests/integration/test_acceptance_probes.py:25`), so the multilingual probe
   runs and fails in the no-services unit gate — violates TESTING.md's own
   unit="no services" contract. Skip-without-services or drop the unit marker.
4. **Order-dependent failures:** `test_queue_keys_are_namespaced_per_env` and
   `test_claim_keys_are_namespaced_like_the_rest` fail in the full run but pass
   focused and grouped (30/30) — a test-isolation leak; polluter unknown.
5. **Contract drift / stale pins:**
   - Qdrant store requires query identity at the dense boundary
     (`qdrant_store.py:1037`) but the contract test doesn't supply it.
   - W7 rollback probe dies `sqlite3.OperationalError: incomplete input`
     (`test_w7_parity_off_rollback.py:495`) — the probe's statement splitter vs
     current migration content.
   - `test_a_store_failure_fails_closed` pins the old swallow-and-empty
     contract while `PostgresChunkLookup.get_many` now raises a typed
     `CanonicalStoreUnavailable` — the Q0 "no silent errors" philosophy argues
     the new behavior is right; someone must decide the canonical contract and
     update the other side.
6. **`.env.example` is now actively misleading.** It contains **zero**
   `WORKFLOW_*`/`AGENT_STORE_DB` entries, and its header still claims *"Every
   value has a working default in docker-compose.yml"* — false since T-R01/T-R05
   and now also for the four workflow vars. README quickstart and `make up` don't
   mention the export ceremony either, and nothing documents that the exported
   paths must live under the mounted `/app/.workflow/...` (an arbitrary path
   silently lands on the ephemeral container filesystem — shared but not
   persistent). Also: `get_draft_store()` is still called outside the handler's
   error mapping (`workflows/router.py:189`) — unreachable via shipped compose
   (fail-closed earlier), but a raw `ValueError`→500 under `make api-dev`.
7. **T-I03's behavioral tests are marked `unit` but require the live Postgres**
   (they fail, not skip, without it) — same marker-contract issue as the
   acceptance probes in P2-3; in a no-services CI run these will fail.

## 4. Systemic observations

- **Process > product risk.** 277 micro-tickets, 248 done (89.5%), enormous
  coordination output — yet the core promise (upload→answer with real citations
  on live services, at Q9 quality) is still unproven. The ticket machine
  produces merges; acceptance requires whole-suite gates + live runs +
  representative eval, which only became possible today. Shift the effort ratio.
- **The recurring failure mode is now cheap to fix.** "Focused tests green +
  ruff+mypy clean (re-run)" in handoffs is honest per-file but false tree-wide
  (F2/F3). With collection fixed, wire the full gate into the loop and this
  class of regression disappears.
- **Evidence culture is a genuine strength** — incident logs, signed handoffs,
  honest admissions ("my A-wave's 'suites green' claims were per-file"),
  revert-with-preserve discipline, triage-then-verify. Keep it; route it into
  gates instead of prose.
- **Multi-writer review hazard:** HEAD advanced ≥3 times during this review
  (b5e957c → d04d469-lineage → 94c2747), and one fix landed mid-review. Reviews
  must pin snapshot hashes (done here); DONE claims should cite the hash they
  were verified against.

## 5. Recommended order of operations

1. **Finish T-I01 properly:** shared named volume for the agent DB; fail-closed
   default (or admission refusal) rather than silent in-memory fallback; atomic
   claim; WAL + busy_timeout; lease parity; evidence store inside the seam;
   cross-process claim test.
2. **Clear the regression set (small, unblocks 5 of 12):** add `POLICY_BLOCKED`
   to `ReasonCode`; accept/forward `sensitive` in the ask factory contract;
   remove the `os.environ` API-key read from `test_openrouter_provider.py`.
3. **Restore static gates:** resolve the duplicate `rerank` module (exclude or
   `--explicit-package-bases`); ruff clean including the pending `evals/bfcl_*`
   files before they are committed.
4. **Drive the suite 12→0 and adopt it as the DONE gate** (polluter hunt for the
   order-dependent pair; reconcile the three contract drifts/pins).
5. **Coordinator-gated redeploy of the current tree** with the new required
   exports; verify `/version` carries git_sha and store modes show persistent
   stores; re-check readiness disclosure (workflow/agent capabilities).
6. **Close exposure:** loopback-bind 8000/3001 (or nginx TLS with auth), add
   DOCKER-USER restrictions, restrict 8095.
7. **Q8 corpus, then re-run the eval** (EN/RO/PT/table/multi/unanswerable);
   run the T-G01 A/B with an adversarial hallucination fixture guarding the
   no-fabrication guarantee.

## Handoff (project convention)

```text
TASK: Review Super RAG project (post-I-wave follow-up)
CHANGE: This review document only; no product code touched.
WHY: Independently verify whether the 2026-10-15 I-wave fixes close the P1/P2 findings, and assess overall project state against the operative plan.
REUSED: Existing suites, compose, live endpoints; no copied implementation.
BUILT: Review evidence only (full-suite run, focused repros, port/DNAT probes, fix verification).
VERIFY: Commands and outcomes inline above (full suite 1861/12, vitest 225/225, tsc clean, ss/ufw/DOCKER-USER/curl evidence, source line references).
RISKS: Live multi-agent tree — claims pinned to b5e957c (suite) and 94c2747 (source); no deployment, restart, external messages, or paid calls performed.
```
