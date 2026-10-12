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
