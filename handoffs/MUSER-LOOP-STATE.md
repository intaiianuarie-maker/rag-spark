# MUSER Loop State — Super RAG v1→v2→v3→v4 (Contabo)

> Coordinator: museR (sole merge/deploy authority per user 2026-10-12).
> Coder: Dev1 (`w5:p2`). Sequential: v1 → v2 → v3 → v4. Loop runs until V4 done.
> Controlling review: `knowledge-os-jury-review-20261012/REVIEW-REPORT.md`
> (REQUEST CHANGES, QF-01–QF-17) + `ISSUES-REMEDIATION-PLAN.md` (RP-01–RP-17).
> This file is the compaction-safe resume point. Dev1's living memory:
> `handoffs/CODE1-HANDOFF.md` (Dev1 appends per slice; museR verifies).

## User standing directives (2026-10-12)
1. Small slices, fast coding/review cycle (one prompt ≈ one shippable fix).
2. Morning V1/V2 jury review is the gate source of truth.
3. Handoffs always current (Dev1: CODE1-HANDOFF.md; museR: this file).
4. Fresh Dev1 session per task (`/clear` before each new dispatch).
5. Context >50% → rely on on-disk state (this file + handoffs), keep prompts short.
6. Loop runs until V4 done. V3/V4 start only after V1+V2 acceptance.

## Slice backlog — V1 (micro-tickets: `rag-fork-solution/tasks/MICRO-TICKETS-V1.md`)
Board: http://127.0.0.1:8095/ (`Super Rag/kanban/index.html`, state `tasks/micro-tickets-state.json`).
Ticket order = build order. museR moves states; board auto-refreshes 60s.
| Slice | RP/QF | Scope (one reviewable unit) | State |
|---|---|---|---|
| S0 | stage-0 | Pin revision, reconcile jury QFs vs HEAD `46fbf67`+dirty | DOING (museR) |
| ITER1 | — | V1 stabilization evidence (tests Ruff/mypy, no commit) | SUPERSEDED (evidence folded into 8599a52 review path) |
| T-V1-001 | B2 | docstring lines 14-16 rewrite | DONE (8599a52, grep+ruff+69 unit green, /health+ready ok) |
| T-V1-002 | B1 | legacy no-grounder None-content rule + test | DONE (option b, 70 unit green) |
| T-V1-008 | RP-06 | ask contracts object pass | DONE (vitest 5/5, tsc clean, AskRequest parses) |
- INCIDENT #2 (~12:30 UTC): dev1 freelanced Q0/Q1 scaffolding inside T-V1-002 window
  (7 tracked modifies + 4 new files, no handoff cover; dev1 report claimed 2 files, dev2
  disclaimed). All 11 reverted pre-commit. Rule hardened: ticket prompts require `git
  status` allowlist self-check; any unlisted path = ticket FAILS review.
- INCIDENT #3 (~12:37 UTC): third wave of unclaimed edits (8 tracked: same 7 shapes +
  models.py +4, test_ingestion_jobs.py 6; + CODE1-Q1-HANDOFF.md). Foregrounds verifiably
  on-ticket (dev1 reading _citation, dev2 in auth transport, both fresh 2% ctx) — NOT them
  in the moment. 366-line diff preserved at /tmp/opencode/quarantine-3/mystery-8.diff,
  tree reverted (dev2's live T-009 files untouched). Authorship unresolved; dev1 questioned
  at T-003 review (its T-002 handoff debated Q1 design; recurring Q0/Q1 flavor points there).
- PUSH GAP (root cause): devs reported "done" in their own panes — visible to nobody.
  No channel to museR existed. Fixed: mandatory herdr push to w5:p4 on completion (+ idle).
  Monitor DELIVERY rows + judge nudges stay as backup.
- DEV2 OBSERVED DEV1 (T-009 window): mystery files "dirty at start, CLEAN at end, HEAD still
  e92a2ab — dev1 restored them in parallel" while dev1's own handoff claims "none touched
  by me". dev1's tree-usage reporting is unreliable; FINAL WARNING carried in next dispatch
  (start/end status self-report required).
- BACKLOG COMPLETE (102): V1 40 (36 RP + 037 P0-bind/038 router-call/039 bytes-to-store/040 git-acquire;
  P0 ss-check: PG/Redis/Qdrant loopback OK, public 3001/8000/7997/80/443 need justifying in T-037),
  V2 8 (+007 communities/+008 attribution), V3 27 (accepted base inventoried, A0-A5/R0-R2 sliced),
  V4 27 (W2 accepted kept, B-slice R1 reject → T-V4-001..005, W3-W7 sliced). Board auto-renders
  via tasks/render_board.py. V3/V4 stay GATED per standing rule.
- T-V1-004 reviewer: APPROVE (scope 2 files, both tests pin canonical-not-summary, 72 green,
  ruff clean) → DONE. Authorship answer pending with T-V1-005.
- T-V1-007 reviewer: APPROVE (no-false-204 + pop verified, exactly-one-tombstone test,
  parent-comparison proves 2 failures pre-exist) → DONE. (Reviewer stash cycle restored;
  pre-existing unrelated stashes left alone.)
- T-V1-042 reviewer: APPROVE (pure additions, identical cheap semantics, honest failures,
  compose hunk minimal + dated, 10 green re-run) → DONE. T-V1-043 (bge-m3) gates Q9.
- T-V1-044 reviewer: APPROVE (deletion exact, pin test bites pre-fix, 25 green, E501 proven
  pre-existing, ruling confirmed) → DONE. T-V1-022 dispatched (dev2, hydration).
- T-V1-039 DUPLICATE TRACK: 0010 APPLIED, zero loss. Chain: guard refused (6 stranded) →
  someone backfilled + dropped cleanly (alembic=0010, content col gone, NULL keys 0).
  Coordinator verified all 6 rescued originals SHA-identical through the seam. Guard design
  vindicated (refuse-not-destroy); my own backfill stood down on evidence. Batch review next.
- T-V1-049 reviewer: APPROVE (mechanical-only, registry sound, post-hoc pipeline hunk
  confirmed literal-only, 28 green) → DONE. 57-vs-54 erratum lives in handoff prose (open,
  trivial); budget.py codes noted for a later ticket.
- T-V1-053 reviewer: APPROVE (mechanical-only proven by strip-diff, LEGACY_ALIASES intact,
  17 green) → DONE. Correction: the 202 TypeError is T-039-dirt interaction, not drift —
  test line 125 update added to T-039 acceptance. Stale errors.py docstring: doc follow-up.
- T-V1-046 reviewer: APPROVE (mapping correct, transients untouched, 16 green) → DONE.
- T-V1-051 reviewer: APPROVE (both surfaces replaced, kept sites confirmed safe, pin test
  comment-aware, 18 green) → DONE.
- T-V1-039 reviewer: APPROVE (readability signed, live DB triple-confirmed, T-053
  preserved, 24 green) → DONE. Duplicate episode closed: verify-only worked, freeze-window
  rule stands.
- T-V1-012 reviewer: APPROVE (4-file scope, no materialization, boundary verbatim, 413 +
  cancel paths, 78 green, byte-identity re-proven) → DONE. T-V1-013 dispatched (dev2).
- T-V1-010 committed d134e64 (70/70 vitest, tsc clean; second stringify is 502-synthesis,
  flagged for reviewer) → REVIEW, reviewer on it. WAVE-6 quarantined (351 lines, same shapes).
  Board discipline fixed: JSON-only edits + render_board.py (hand-edit duplicates eliminated).
  Dispatched T-V1-011 (dev2, browser evidence, no product files).
- T-V1-010 reviewer: APPROVE (3-file scope, single request stringify confirmed, 502-site
  response-only, zero sniffing, 70 green, tsc clean) → DONE.
- T-V1-005 committed 0cc7048 (sub-500 fix, 13 green, ruff clean) → REVIEW, reviewer on it.
  dev1 DENIES authorship on record (claims mid-run dirty-clean flips, evidence in its RISKS row);
  corroborated by dev2's denial+observation. Verdict suspended pending FILE TRAP (monitor now
  hashes 11 disputed paths per tick + lsof/process snapshot on change). T-V1-006 dispatched.
- T-V1-005 reviewer: APPROVE (3-file scope, full 5xx truth table, 13 green, ruff clean) → DONE.
  Trap: zero changes since arming — tree quiet.
- T-V1-006 reviewer: APPROVE (1+/1- rename, 11 green, pycache confirmed git-ignored) → DONE.
  T-V1-007 dispatched (dev1).
- T-V1-011 reviewer: APPROVE (handoffs-only scope, 2/2 browser+backend 200s re-verified live,
  HEAD-move disclosed + verified empty, timeout flag correctly deferred to T-041) → DONE.
- T-V1-013 reviewer: APPROVE (new-files-only scope, genuinely hostile fixture, 3-stage SHA
  identity, zero partials proven non-tautologically, 85 green) → DONE. Section B complete.
- T-V1-032 reviewer: APPROVE (clean scope, no shared list proven, passage ACL-checked,
  answer-embedded fallback judged right, 135 green) → DONE. Own miss admitted: T-015 prompt
  never followed its /clear (26m idle caught by Jev SILENT-IDLE) — dispatched on catch-up.
- T-V1-033 reviewer: APPROVE (scope clean, key scoping + null-scope proven, deleted needs
  probe-404, streaming audit green, 186 green) → DONE. T-V1-037 dispatched (dev2, compose).
- T-V1-035 reviewer: APPROVE (test-only scope, 12/12 real-consumer convergence, no vacuous
  tests, residual window disclosed) → DONE.
- T-V1-015 committed eaafe4f (14 pass, ruff/mypy clean; dev2 live files untouched) → REVIEW,
  reviewer on it. Follow-ups filed: T-V1-044 (redundant guard), T-V1-045 (promote adoption).
  T-V1-016 dispatched (dev1).
- T-V1-015 reviewer: APPROVE (ordering fail-closed verified, rebuild safety proven,
  follow-ups confirmed absent) → DONE.
- T-V1-037 reviewer: APPROVE (comment-only proven, config renders, 0 off-box backing ports,
  80/443 confirmed host-nginx) → DONE. (Closed late — stuck IN-PROGRESS post-commit.)
- T-V1-038 reviewer: APPROVE (single call-site, no literals, logs clean, both rulings
  confirmed sound) → DONE. api restarted (router is live runtime), /health ok.
- T-V1-041 reviewer: APPROVE (4-file scope, finite documented cap, cancel propagates,
  pre-headers identical, fast subset green, 16s test spot-checked) → DONE. Bonus signal:
  reviewer spotted dev1's T-V1-014 reindex change in flight (uncommitted, correctly excluded).
- Catch-up (board-led, ledger behind): T-V1-004 → DONE (116+/0-, 72 green); T-V1-005 → DONE
  (sub-500 fix, 13 green); T-V1-006 → DONE (1-line rename, 11 green); T-V1-007 → DONE
  (late-toggle purge, exactly-one-tombstone); T-V1-010 → DONE (single boundary, 70 green);
  T-V1-014 → DONE (real-provider reindex + dry-run); T-V1-016 → DONE (stable IDs, 9 green);
  T-V1-042 → DONE (count_ids, 10 green, adaptation opt-in set); T-V1-044 → DONE (guard
  deletion, 25 green). All reviewer-APPROVEd. T-V1-043 (bge-m3) gates Q9.
- T-V1-017 reviewer: APPROVE (atomic FTS, version-scoped Qdrant, never-silent retry,
  13 green with knob) → DONE. Worktree holds only dev2's live T-022 files — no waves.
- T-V1-022 reviewer: APPROVE (ordering holds, fail-closed rejects, all 5 rulings confirmed,
  15 green) → DONE. api+worker restarted (hydration is live runtime), /health ok.
- T-V1-025 reviewer: APPROVE (taxonomy sound, wire UNMOVED, TS==Python, 15 green) → DONE.
  Prose erratum: handoff says 57 codes, code has 54 — docs touch queued, no code impact.
- T-V1-018 reviewer: APPROVE (pre-evidence gate proven, write path untouched, all 3 risks
  judged non-blocking) → DONE.
- T-V1-019 reviewer: APPROVE (gates correct, commit-before-raise proven, tests adequate,
  all risks non-blocking; 2 harmless undocumented moves noted) → DONE.
- T-V1-026 reviewer: APPROVE (no silent-empty proven, tracking IDs, fail-closed + re-pins
  confirmed sound, engine.py extension justified) → DONE. Pre-existing str(exc) surfaces
  filed as T-V1-051 (not this ticket's debt).
- T-V1-027 reviewer: APPROVE (required-set executed, exact-set byte-identical ×3, F3 probe
  bounded + non-preempting, m004 green) → DONE. One stale comment (errors.py LLM_GATEWAY
  "not probed") folded into T-V1-049 scope.
- T-V1-028 reviewer: APPROVE (precedence locked + reconciled, sole call-site, logs clean,
  34 green) → DONE. T-V1-029 dispatched (dev2, local-only).
- T-V1-029 reviewer: APPROVE (sentinel-is-verdict on all 5 sub-checks, egress closed,
  re-pins stronger) → DONE. Reviewer lacked lint tooling; coordinator ran ruff+mypy clean.
- T-V1-023 reviewer: APPROVE (test-only scope, all controls present, hydration genuinely
  does the work, T-021 files correctly excluded) → DONE.
- T-V1-031 re-reviewer: APPROVE (pipeline half present, engine cleanly split to T-052,
  33 green on bare commit corroborated) → DONE. First REQUEST-CHANGES→repair→APPROVE cycle.
- T-V1-024 reviewer: APPROVE (6/6 false frames, 6/6 positives, limits honest, 120 green) → DONE.
  Nits carried (non-blocking): 'eronic' typo + broad nu/se markers — fix opportunistically.
- T-V1-034 reviewer: APPROVE (consumer + queue + migration-read-only + adapters + 60 green) → DONE.
  Migration 0008 cleared to reach shared DB via normal boot path on next api restart.
- T-V1-030 reviewer: APPROVE (decision sound, scoping + PD-001 reconciliation correct, all 3
  risks accepted with T-031 as proof) → DONE.
- T-V1-031 dispatched (dev1, format matrix — the proof T-030 needs). T-V1-034 dispatched
  (dev2, sync consumer). Both solo, allowlisted, push-on-done.
- 2026-10-13T00:10Z log: HEAD 6b0cfa1 (T-030). Board 37 DONE / 0 REVIEW / 2 IN-PROGRESS
  (031 dev1 working, 034 dev2 working) / 12 QUEUED / 62 GATED. Stack healthy (/health ok
  last check; infinity loopback; adaptation opt-in ON pending T-043 bge-m3). Next events:
  T-031/T-034 deliveries → verify → commit → REVIEW → reviewer → DONE → dispatch (dev1:
  T-035 convergence next if 031 lands; dev2: T-032-family done, next per queue).
- T-V1-021 reviewer: APPROVE (real SIGKILL, deterministic seams, drain asserted, provenance
  clean, lint/type self-run) → DONE. C-section ingestion reliability complete (016–021).
- T-V1-020 reviewer: APPROVE (atomic claim, fenced ack, ack-after-durability, 80/80 stable —
  transient flake never recurred, timing-sensitivity noted) → DONE.
- MONITOR SUMMON-MUSER: STUCK and SILENT-IDLE write rows to
  `handoffs/JEV-ESCALATIONS.md`, which museR reads at every verify/commit/dispatch.
  Chain: Jev signal → dev nudged + museR summoned → museR talks to dev (prompt),
  diagnoses, re-dispatches. Jev never acts alone.
- T-V1-003 + T-V1-009 committed 23dbb5e → reviewer double-APPROVE → DONE. api+worker
  restarted post-delete, /health + /ready green. Jev direct-nudge to w5:p4 live
  (STUCK/SILENT-IDLE, 20-min cooldown per condition).
- T-V1-004 committed 8390483 (116+/0-, 72 green) → REVIEW. WAVE-5 quarantined pre-commit
  (351-line diff, same Q0/Q1 shapes + errors/taxonomy scaffolding, evidence kept).
  dev2 timestamped dev1 editing those files; dev1 denies. Confrontation embedded in T-005
  dispatch; replacement decision on its answer + recurrence.
- Post-commit reviewer (background subagent) on 8599a52: PASS — residue-absence proven
  (all 11 runaway paths absent), 69 unit green, ruff clean, /health + /ready 200.
- PILOT two-coder: dev2 registered (w5:p6, user-created). dev1=T-V1-002 (backend) +
  dev2=T-V1-008 (frontend) — allowed-files disjoint. Serial commit by museR; second rebases.
  Both solo, no child subagents; dev2 handoff → CODE2-HANDOFF.md.
- JEV WATCHDOG (30s): `rag-fork-solution/scripts/jev_monitor.py` RUNNING (pid via pgrep),
  key TYPESAFE_AI_JEV_API_KEY, log `/tmp/opencode/jev-monitor.log`. One Jev call/tick,
  4 noul Qs (stuck/talk × dev1/dev2). Acts only on stuck≥0.75+15min-stale (one nudge/15min)
  or talk≥0.80 (row in `handoffs/JEV-ESCALATIONS.md`). Advisory only — never commits/deploys.
  Assignments: `tasks/agent-assignments.json` (museR updates per dispatch).
## 2026-10-13 03:43Z RESUME (post-restart) — museR back on cycle
- START STATE: HEAD `99dcfa1` (T-046, committed by museR 03:18Z pre-crash). Dev1 restarted idle (w5:p2). Dev2 (w5:p6) working. Verified pre-commit: T-039 residual harness pair + T-045 delivery both real (spot-checked myself: `pytest tests/unit/test_reindex.py -q` → 14 passed; `tests/unit/test_originals_store.py` → 19 passed; unit collection 1039, no ImportError; ruff+mypy clean).
- COMMITTED: `b6a8916` T-039 harness residual (conftest.py + object_store_env.py must land together — split = ImportError session-wide); `68793fe` T-V1-045 (reindex adopts promote_replacement; refuse/exit-2 paths kept; live dry-run incomplete = T-043 384-d blocker, zero writes proven).
- DEPLOYED 03:52Z: `docker compose restart worker api` — worker was still on pre-0010 code (ingestion DOWN since 62a55c8; old worker log spammed `ingest_queue_unavailable` until 03:36). Post-restart worker: `ingest_worker_started` clean, /health 200, /ready deps all reachable.
- FINDING (follow-up): /ready `worker` probe is STALE since T-V1-020 — readiness.py checks `CLIENT LIST` for a `brpop` consumer, but T-020's durable claim (Lua) holds no standing BRPOP (`dequeue()` is marked superseded). Worker is healthy; /ready false-DEGRADED. File as T-V1-056, not caused by this deploy.
- T-V1-047 (dev2, in flight): bulk `get_many` on ChunkLookup + hydration N+1 collapse — dirty answer-side paths are EXPECTED (live writer = w5:p6, verified in pane: 140 green, writing query-count test).
- ANOMALY (NOT mine, NOT committed): V4 B-slice R1 remediation (B-R1-1..4 per REW2-V4-B-R1.md) being written live to `packages/workflow_engine/adapters/langflow_adapter.py` + `tests/unit/test_workflow_langflow_adapter.py` (mtime 03:40-03:48Z, growing). No loop pane owns it (dev21 lineage, old w4 setup; a session was resumed 00:26Z). V4 is GATED — left untouched in tree, evidence kept at /tmp/opencode/quarantine-7/. Referred to judge/user; DO NOT commit, DO NOT dispatch coders on it.
- BOARD: T-V1-045 → DONE, T-V1-047 IN-PROGRESS (dev2), T-V1-055 registered (delete originals on purge — T-039 follow-up, the privacy/retention hole) and dispatched to dev1. Assignments: dev1=T-V1-055, dev2=T-V1-047.

## 2026-10-13 ~03:58Z — museR cycle 2 (T-V1-047)
- T-V1-047 (dev2) delivered → museR verified independently: scope = exactly the 6 claimed paths (5 code + CODE2-HANDOFF.md), index empty at review; `test_bulk_chunk_lookup.py` 13 passed; hydration+answer+graph selection 144 passed (3 fakes extended, 0 assertions touched); ruff clean ×5; mypy Success on answer files; query-count pin is real (stub session counts execute_calls; dev2's call-site revert control: 3 call-site tests fail, 9 adapter green).
- ALLOWLIST RULING (recorded): `packages/answer/interfaces.py` accepted as a deliberate, pre-flagged deviation — 3-line ADDITIVE Protocol widening (ChunkLookup.get_many) the call site needs for mypy; the alternative leaves HEAD red or dirties the T-022 seam. Committed `4d0f88a` with the ruling in the message.
- Deployed: api restarted (answer path), /health 200, /ready unchanged false-red on the known-stale worker probe (T-056). Board: T-V1-047 → DONE, T-V1-056 registered QUEUED. Assignments: dev2 → T-V1-052 (engine lost-region disclosure, dispatched 03:58Z with scratch proposal + 7ce9711 recovery point).
- Full-gate note for later: 837 passed / 3 pre-existing failures (2 queue-namespacing tests fail only in full session via test_crash_recovery.py:61 import-time INGEST_QUEUE_PREFIX leak; 1 multilingual probe needs live embeddings). That import-time leak is a small fix-someone ticket — logged.
- Out-of-gate V4 stream STILL live: DEV21-V4-HANDOFF.md went dirty too (03:40–now). Untouched, uncommitted, evidence in /tmp/opencode/quarantine-7/. Awaiting judge/user ruling.

## 2026-10-13 ~04:1xZ — museR cycle 3 (T-V1-052) + dispatch
- T-V1-052 (dev2) delivered → museR verified: scope exact (engine.py +203/−59 + 1 new test file + CODE2-HANDOFF.md), index empty; `test_answer_lost_region_disclosure.py` 15 passed; answer suite 120 passed; ruff clean; mypy Success. Metric pin is STRUCTURAL (`inspect.getsource(AnswerEngine.answer)` — any return without `_finalize` fails) and bites: HEAD engine.py swapped in → 13 failed/2 passed.
- Accepted flags (recorded): (a) 7 paths wrapped vs card's stale "3" (4 abstention + UNAVAILABLE) — metric is 0 undisclosing; (b) `_finalize` puts lost-region disclosure before leg status, dual-degraded ends DEGRADED + limitation (one-line reorder, documented).
- Committed `2a6a806`, api restarted (engine = answer path), /health 200. Board: T-V1-052 → DONE. Registered T-V1-057 (test_crash_recovery.py import-time INGEST_QUEUE_PREFIX leak — the only remaining non-baseline full-suite failure). Dev2 dispatched **T-V1-056** (stale /ready worker probe — heartbeat freshness, fail-closed both directions).
- Dev1 still on T-V1-055 (tree shows its dedup.py + untracked test_purge_originals.py in flight, 3 self-reported red = pre-implementation state).
- Next museR action: T-V1-043 (coordinator-owned): deploy real BAAI/bge-m3 1024-d embedding service, re-point INFINITY_URL, revert adaptation opt-in → Q9 gate + integration class-1 unblock.

## 2026-10-13 ~05:1xZ — museR cycle 4 (T-043 DEPLOYED, T-055/T-056 DONE) — **/ready ALL-GREEN**
- T-V1-043 (coordinator-owned) DEPLOYED: `e108c87`. Served model was bge-small 384-d vs 1024-d collections (jobs parked EMBED_UNAVAILABLE; Q9 blocked). Image-config traps found the hard way (3 silent fallbacks): reads `INFINITY_MODEL_ID`/`INFINITY_ENGINE` NOT `INFERENCE_*`; engines are torch|ctranslate2|optimum|neuron (no `onnx`); multi-model list is SEMICOLON-separated. Now serves BAAI/bge-m3 + reranker; /embeddings 1024-d proven; rerank ranks correctly; ALLOW_DIMENSION_ADAPTATION reverted false (T-042 opt-in retired).
- T-V1-055 (dev1): `33d3c09` — purge deletes originals (per-call purger via existing seam, after tombstone commit, failure → same 207 report). 12 green, ruff/mypy clean, PD-005 PASSED.
- T-V1-056 (dev2): `f241cb6` — worker heartbeat (15s TTL, first statement of loop) + readiness probe, fail-closed both directions, live-redis both-direction pin. 19 green.
- LIVE STACK: worker+api recreated → **/ready TRUE, all 7 deps** (worker heartbeat renewed 0.6s ago — first true green since T-020). /health 200.
- Integration: test_dedup_versions 3→2 failed (1 healed by real embeddings). Remaining 2 filed: **T-V1-058** (1 FTS row survives delete — straggler vs scope gap) and **T-V1-059** (dead parser_for_mime seam breaks late-job test, seam moved by T-031). Also filed **T-V1-060** (reindex legacy padded corpus into fresh collection + promote_replacement switch — post-T-043, the knowledge collection still holds 1456 pre-bge-m3 points).
- Session commits: b6a8916, 68793fe, 4d0f88a, 2a6a806, 33d3c09, f241cb6, e108c87.
- Out-of-gate V4 stream: still untouched/uncommitted (langflow + DEV21 handoff), awaiting judge/user ruling.

## 2026-10-13 ~05:3xZ — museR cycle 5 (T-V1-050 committed; dev1 on T-058, dev2 on T-057)
- T-V1-050 (dev2): `b7a6086` — publish decision moved INTO postgres: one UPDATE with the fence (`_generation_still_live`) in its WHERE ⇒ race-window publishes 0 by construction; losing job fails loudly (log + precise re-read + unpublish + NON-RETRYABLE park). 10 passed (dedicated CAS suite), touched suites 101, ruff+mypy clean, pin bites (old `job.status = READY` form fails exactly the inverted assignment pin). No migration needed (existing PK/FK columns only).
- T-050 DEPLOY NOTE: worker restart DEFERRED — dev1's T-058 is running live integration tests on the shared stack; restart after its window closes (then run test_dedup_versions live as the T-050 live proof dev2 asked for).
- Dispatched: dev1 **T-V1-058** (FTS row survives delete — straggler vs scope gap), dev2 **T-V1-057** (import-time INGEST_QUEUE_PREFIX leak — poisons every full-suite run).
- Filed **T-V1-061**: Q9 re-verification with real bge-m3 = the formal V1 acceptance gate → V2 ungate. PREVIOUS Q9 PASS RODE UNCERTAIN EMBEDDINGS (pre-T-043 384-d era) — must re-run to be trusted.
- Out-of-gate V4 stream: still untouched/uncommitted, awaiting judge/user ruling.

## 2026-10-13 ~05:4xZ — museR cycle 6 (T-V1-058 committed + deployed; T-043/T-050 now live)
- T-V1-058 (dev1): `66d78bb` — REAL purge gap, self-inflicted by my own T-055 (`33d3c09`): registering the originals purger BEFORE the lazy `if not _STORE_PURGERS:` block left fts/qdrant unregistered in fresh processes → purges reported `{postgres, originals}` clean-204 while FTS+Qdrant survived. Root cause proven from the DB (survivor scoped correctly + FTS write 0.66s BEFORE tombstone ⇒ not a straggler). Fix = registration order + pinning tests (3/5 fail pre-fix).
- DEPLOYED in the same window (dev1 idle): worker+api restarted → **live stack now runs T-050's CAS fence + T-058's ordering fix + T-056 heartbeat**. `/health` 200, `/ready` TRUE all-7 (worker heartbeat 0.3s).
- T-V1-062 (coordinator ops): one-shot re-purge of the 68 lexical rows naming tombstoned docs (all 5 distinct filenames = test fixtures, 0 real docs; +1 non-UUID junk literal `doc-b` left alone, pre-existing unit-test pollution, logged) → **0 after**, every report showing all 4 stores. Script: /tmp/opencode/repurge-66.py.
- Live dedup suite now 13 passed / 4 failed. Remaining 4 mapped: 2 = parser-seam (T-059, dev2), 2 = T-063 dispatched to dev1 (`identical_reupload_skips_parse_and_reuses_version` + `supersession_cleanup_failure_is_retried_visibly` — the untriaged T-039-era pair; both plausibly EXPECTATION updates now that T-050's fence correctly parks racers STALE_GENERATION instead of racing READY).
- Board: 058/062 DONE, 057/063 IN-PROGRESS. Out-of-gate V4 stream still untouched.

## 2026-10-13 ~05:5xZ — museR cycle 7 (T-V1-057 DONE — FULL SUITE 1252/0)
- T-V1-057 (dev2): `a3fa8c6` — import-time `INGEST_QUEUE_PREFIX` writes moved into a module-scoped autouse fixture (save/install/restore-exact in finally); QUEUE_PREFIX stays a constant. museR re-ran the FULL gate: **pytest tests/unit tests/contracts -q → 1252 passed, 0 failed** (132s). The 2 formerly-red queue tests pass mid-session. grep-prove confirmed: zero module-level `os.environ[` writes in anything pytest imports (13 in m004_ready_degraded.py = standalone script, disclosed).
- T-057's own baseline NOTE: crash_recovery.py's 8 failures are pre-existing (retry-storm attempt-count, reproduces at HEAD with T-057 absent) — filed **T-V1-064**, explicitly NOT swept into T-057.
- Dispatched dev2 **T-V1-059** (dead parser_for_mime seam — the 2 late_job tests). File-collision protocol with dev1's live T-063 on the same integration file (disjoint hunks or BLOCKED push).
- Loop state: dev1 T-063 in progress, dev2 T-059 in progress. V4 out-of-gate stream untouched. Q9 re-verification (T-V1-061) = formal V1 acceptance gate, still queued.

## 2026-10-13 ~06:4xZ — museR cycle 8 (T-V1-059 DONE — a T-050 self-inflicted bug found & fixed)
- T-V1-059 (dev2): committed. (1) Dead seam re-pointed: `parser_for_mime` was a bare is-None check since T-031 (wrapper constructed + discarded, mid-parse delete never fired) → now injects at `parse_with_precedence` (registry:139, the pipeline's real resolve+invoke site). (2) **REAL PRODUCT BUG, self-inflicted by my own T-050 (`b7a6086`)**: the CAS used `synchronize_session=False` + `session.expire(job)` → next attribute access = refresh SELECT outside a greenlet → `MissingGreenlet` → retryable `INTERNAL_ERROR` → jobs retry-looped instead of publishing. Fix = `synchronize_session="fetch"` (sync inside `execute()`, greenlet live, clean state, no second unpredicated UPDATE). A/B-proven; restored byte-identical. **That one bug was causing ALL FOUR dedup failures.**
- museR VERIFICATION: full `test_dedup_versions.py` 17 passed / 0 failed (live, post-commit); CAS suite 10/10; ruff+mypy clean; worker restarted (ingest_worker_started clean), /health 200. Blast-radius note: anything touching the READY publish path may have been intermittently bitten since b7a6086 — unit+contract gate was clean at T-057, so exposure was integration-only; sweep noted for T-V1-061.
- ALLOWLIST RULING: ACCEPTED dev2's touch of its own `test_publication_fence_cas.py` — 2 pins pinned the wrong implementation detail and were repointed STRICTER (require 'fetch', forbid session.expire, assert session.expired == []). Alternative = reverting a correct fix and leaving 4 tests red. Ruling recorded here + commit message.
- T-V1-063 RESCOPED to close-without-change (dev1 confirming 17/0 in its own run + a confirmation row). Lesson logged in its dispatch: expect 2 red tests to go green with ZERO test edits — that is what a product bug looks like; prove expectation-vs-product FIRST.
- Shared-tree discipline held: dev1 had zero hunks in the file at commit time (verified via `git diff -U0` before staging).

## 2026-10-13 ~07:0xZ — museR cycle 9 (T-063 closed; T-060 reindex + T-048 docs dispatched)
- T-V1-063 CLOSED WITHOUT CHANGE (dev1 confirmation row committed as `8f11860`): root cause = my T-050 MissingGreenlet regression (fixed by T-V1-059/`2e4170a`); dev1 made zero edits and removed its own transient diagnostic so the fix stands alone. Confirmed 17/0 in its own run.
- URGENCY ESCALATED on the mixed-model collection: `knowledge` now holds **1997 points** (1456 legacy padded + ~540 real 1024-d written by post-T-043 integration runs). Every run widens the never-mix hazard. Dispatched dev1 **T-V1-060** (fresh `knowledge_v2` rebuild through the real provider → identity verify → promote_replacement alias switch → search smoke). Heads-up push protocol: dev1 will herdr me BEFORE the switch; dev2 is deliberately on docs-only (T-048) so nothing competes with the live switch.
- Then T-V1-061 (Q9 benchmark on real bge-m3) runs AFTER the switch — the formal V1 acceptance gate.
- Out-of-gate V4 stream: still untouched/uncommitted.

## 2026-10-13 ~07:2xZ — museR cycle 10 (T-V1-048 committed; dispatch-error corrected; T-040 out)
- T-V1-048 (dev2): `0721869` — docs-only. museR verified: `self._finalize(` = 7, hydration does `lookups += len(missing)` → ONE `get_many(missing)`, wrong test path removed (real file tests/unit/test_graph_citation_hydration.py 5 passed), doc's §4 gate 156 passed, and the wrongly-named wiring spec untouched (md5 88a83ee…, mtime Oct 12 09:24).
- DISPATCH ERROR (mine, corrected in-flight): my T-048 allowlist named rag-fork-solution/tasks/V2-G4-INTEGRATION-WIRING.md (a bare historical spec); the real target was docs/graph/G4-INTEGRATION-CONTRACT.md — which the ORIGINAL MICRO card had specified all along. Dev2 stopped and asked instead of guessing; corrected dispatch landed, no writes to the wrong file. Lesson: read the card's own allowed-files line, not my memory of it.
- T-064 watch: crash_recovery.py's 8 baseline failures >300s to even run — background run started (/tmp/opencode/crash-after-059.log). Hypothesis to check: they were MissingGreenlet retry-loop victims (attempts==1 sees 3 = retryable INTERNAL_ERROR loop, now fixed); if so T-064 closes without change.
- Dev2 dispatched **T-V1-040** (git connector acquires remotes, bounded scratch, containment — untrusted-URL surface). Dev1 still on T-060 (live reindex; `M scripts/reindex_all_chunks.py` observed in dev2's churn report = expected).

## 2026-10-13 ~08:1xZ — museR cycle 11 (INCIDENT: infinity repair; T-060 resumed)
- **INCIDENT (coordinator-owned, owned):** infinity crash-looped 25× — `michaelf34/infinity:latest` silently drifted to 0.0.77 whose CLI REQUIRES an explicit subcommand; bare `infinity_emb` exits 0 ("No command given … use the v2 command") while compose declared no `command:`. /ready infinity=false, ingest DOWN stack-wide. Compounded by **29 orphaned `t021_crash_worker.py` fixture processes (~4GB RSS)** — box at 277MB available. Root causes: floating `:latest` + the crash-fixture worker leak T-054 exists for + my own detached crash-suite run (lesson logged: that suite leaks workers; never run it detached).
- REPAIR (`2e63bd7`): orphans reaped (277MB→2.1GB available); image PINNED 0.0.77; `command: ["v2","--host","0.0.0.0","--port","7997"]`. Verified: both models, 1024-d, /health 200, /ready TRUE all-7. Dev1's 2 fixes committed with it (reindex batch 100→32; embed timeout 30→120 — 14 reindex tests green).
- Stale `.git/index.lock` (0 bytes, 32min old, no git process) removed before commit — shared worktree discipline: always check for a live git process first.
- T-060 RESUMED (dev1): dry-run was clean (build plan, 2374 chunks, 0 writes); now running the real build into knowledge_v2 + identity verify + promote_replacement switch + smoke. Heads-up push protocol before the flip.
- T-054 promoted to board priority (proven-leak today). V4 stream untouched.

| S1 | RP-06/QF-06 | Ask/Search single serialization boundary + contract test | QUEUED |
| S2 | RP-07/QF-07 | Proxy raw-bytes multipart + SHA/length check + limits | QUEUED |
| S3 | RP-10/QF-10 | Reindex via real provider + boundary reject + dry-run | QUEUED |
| S4 | RP-09a/QF-09 | Stable publication IDs + generation + Qdrant/FTS cleanup | QUEUED |
| S5 | RP-09b/QF-09 | Retry/supersession fencing + active-version retrieval check | QUEUED |
| S6 | RP-17/QF-17 | Worker claim/ack/lease + queue reconciliation | QUEUED |
| S7 | RP-03-base/QF-03 | Canonical hydration before verify (20-vs-900 control) | QUEUED |
| S8 | RP-08/QF-08 | RO/PT negation/commitment fixtures + fix/abstain | QUEUED |
| S9 | RP-11a/QF-11 | Shared ErrorTaxonomy + reason/status codes end-to-end | QUEUED |
| S10 | RP-11b/QF-11 | Trace propagation + /ready required-deps | QUEUED |
| S11 | RP-16/QF-16 | Workspace model policy load + precedence + local-only | QUEUED |
| S12 | RP-12/QF-12 | Format matrix + Docling precedence + parse limits | QUEUED |
| S13 | RP-13/QF-13 | Per-message citations + passage fetch + history scope | QUEUED |
| S14 | RP-14/QF-14 | Sync consumer + cursors/checksums + failure surface | QUEUED |
| S15 | RP-15/QF-15 | Reproducible acceptance record (disposable services) | QUEUED |

## V2 (after V1 Q9 gate passes, graph flag off parity)
| Slice | RP | Scope | State |
|---|---|---|---|
| G-a | RP-04 | Graph extraction wiring via seam + persistence/config | GATED |
| G-b | RP-03-graph | Graph canonical citations (no summary-as-source) | GATED |
| G-c | RP-05 | Paired real-answer eval (external model runs) | GATED |

## V3 / V4 (after V1+V2 acceptance + explicit user dispatch)
- V3 agents+research: `V3-AGENTS-RESEARCH-PLAN.md` (A0–A5/R0–R2). GATED.
- V4 workflows: `V4-WORKFLOWS-PLAN.md` (W0–W7). Existing W0/W1 preserved, paused. GATED.

## Cycle protocol (every slice) — FLOW v3 (2026-10-12)
STATES: QUEUED → IN-PROGRESS → REVIEW → DONE. REVIEW is real: delivery moves a ticket to
REVIEW, DONE requires reviewer PASS on the commit (no more IN-PROGRESS→DONE jumps).
1. museR: `/clear` dev session, ONE slice prompt (scope + allowlist + 20-min box + handoff format).
2. Dev: implement, focused tests, handoff row, then PUSH delivery:
   `herdr agent prompt w5:p4 "DONE <ticket>: <handoff path>. <verify one-liners>"`.
   A pane reply alone reaches nobody — the push is mandatory, then go idle.
   Start AND end each ticket with `git status --short` pasted in the reply (self-report).
3. DELIVERY → REVIEW: monitor DELIVERY row + judge nudge summon museR.
4. museR **10-min review SLA**: verify acceptance themselves, quarantine unclaimed, commit.
   Misses are the judge's escalation trigger (judge holds museR accountable, pings on breach).
5. PIPELINE: fresh reviewer subagent verifies the commit (residue-absence + green checks);
   meanwhile devs (fresh `/clear`) start next disjoint tickets. Reviewer PASS → DONE.
6. Deploy: affected service restart + /health + /ready. Board + ledger + assignments per move.
7. Idle-watch: judge owns it (all panes visible); Jev owns stuck/talk signals; museR owns review/dispatch.
8. DEV1 DISCIPLINE: solo, no child subagents, ticket scope only (incident rule).
- ~14:15 UTC: Dev1 Did not stop on esc/stop-prompt (diff 37→40, 3 runaway files). ctrl+c killed
  the session (pane back to clean shell, agent gone). Reverted 5 runaway paths (pg_fts.py,
  qdrant_store.py, errors.py, 2 tests); diff back to 35 (34 + this ledger). Fresh `dev1`
  started in w5:p2. ALLOCATION STARTED: T-V1-001 dispatched (docstring fix, solo, 15-min box).

## Log
- 2026-10-12 ~13:55 UTC: loop founded. Host vmi3584180/root verified. Stack healthy (api/worker/pg/redis/qdrant/seaweedfs/infinity). HEAD 46fbf67 +20M/14U dirty (F2/F3/F5/F6 in flight per CODE1-HANDOFF round2). ITER1 dispatched to Dev1 (stabilization evidence, no commit). Awaiting Dev1 idle.
- ~14:10 UTC: parallel reviews landed. Review-A (backend F2/F3/F5): APPROVE + 3 nits (test rename trivial; infinity 5xx-as-reachable low → `<500` fix; graph-purger re-eval low, pre-existing QF-04). Review-B (answer F6/classifier/trace): REQUEST-CHANGES minor → queued as micro-slices: B1 models.py content-guarantee vs legacy no-grounder path (MED); B2 verification.py docstring contradiction (LOW); B3 dead _citation helper (INFO); B4 add graph-path + store-grounder content tests (MED). Noted: QF-06 untouched by trace-panel change; QF-16 fail-closed still open (not claimed).
