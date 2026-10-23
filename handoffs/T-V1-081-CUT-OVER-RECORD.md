# T-V1-081 — V1 deploy cutover record + pipeline-restart policy

> **Ticket:** T-V1-081 · **Title:** V1 deploy cutover record + pipeline-restart policy
> **State:** QUEUED → DONE (docs-only — no product code touched)
> **Owner:** coordinator (museR / Antigravity, merge authority w4:p17)
> **Cutover timestamp:** 2026-10-14T06:42Z (staleness discovery)
> **Companion record:** `handoffs/RELEASE-RECORD.md` (T-R03; commit digests, config, smoke)

This document has two parts: (1) the **cutover record** that was missing from the
V1 release, and (2) the **pipeline-restart policy** that governs all future
coordinator-owned deploys. It is generated from evidence already in the tree —
no product code is modified here.

---

## 1. Cutover record

### 1.1 Trigger — 06:42Z staleness on T-055/058/080 pipeline code

At **06:42Z** (2026-10-14) a worker-restart cutover was triggered after staleness
was detected in the *running* worker process versus three pipeline commits:

| Ticket | Scope | Commit | Note in `micro-tickets-state.json` |
|---|---|---|---|
| T-V1-055 | Object-storage purge on delete | `33d3c09` (+ harness `b6a8916`) | "board correction: **was stale IN-PROGRESS**" |
| T-V1-058 | 1 FTS row survives document delete (purge gap) | `66d78bb` | "origanals-purger ordering bug fixed; **live worker restarted**" |
| T-V1-080 | Delete-fence precedence (DOCUMENT_DELETED vs INGESTION_ERROR) | `6227b8d` | "both directions green; crash re-run 8/8 by museR" |

The commits above touched the ingest pipeline (`apps/worker/jobs/ingest_pipeline.py`)
and the purge/dedup path (`packages/ingestion/dedup.py`). The running worker had
**not** been cycled after these commits landed, so the `/ready` probe reported
all-green while serving the *pre-staleness* code path. The probe itself was a
false positive: it checked queue liveness but not code-version freshness, which
is exactly the gap closed by T-V1-056.

**Primary evidence source:** `tasks/micro-tickets-state.json`, T-V1-081 note
(line 925) — recorded verbatim:

> "Record tonight's worker-restart cutover (**06:42Z staleness on T-055/058/080
> pipeline code**); establish rule: any apps/worker|packages/ingestion|pipeline
> commit -> worker restart + /ready all-green + heartbeat check;
> **coordinator-owned deploys only**"

### 1.2 The defect this closes

- **Stale `/ready` probe (T-V1-056):** committed `f241cb6`; "Fix stale /ready worker
  probe post T-V1-020 (brpop consumer check)" → deployed live, `/ready` all-green.
- **Stale dispatch tracking (T-V1-055):** "was stale IN-PROGRESS" — board state was
  stale before the purge-ordering fix was re-committed.
- **Stale pipeline code in process (T-055/058/080):** the worker PID still ran the
  pre-`6227b8d`/`66d78bb`/`33d3c09` pipeline, so deletions/tombstones were not yet
  enforced in-process — masked by a `/ready` that only confirmed the brpop consumer
  was reachable, not that it served the current commit.

### 1.3 Cutover action taken

1. **Worker restart** — cycled the ingestion worker process so the T-055/058/080
   pipeline code (tombstone propagation, FTS row cleanup, delete-fence precedence)
   loaded into the live process. (See T-V1-058: "live worker restarted".)
2. **`/ready` all-green recheck** — after restart, `/ready` re-evaluated and
   returned TRUE. RELEASE-RECORD.md records the first sample as false (PG 1212 ms vs
   1500 ms probe budget — a known transient under load) but the recheck passed.
3. **Heartbeat check** — worker heartbeat confirmed at 0.8 s (RELEASE-RECORD.md
   §Smoke); no drift after restart.
4. **Crash-recovery re-run (T-V1-080)** — `crash re-run 8/8 by museR` verified the
   restarted worker survives the disposable-services crash-recovery contract
   (T-V1-021) on the new code.

### 1.4 Post-cutover verification

- `/health` → 200
- `/ready` → TRUE (recheck; first sample false only due to PG probe budget)
- Worker heartbeat → 0.8 s, stable
- Knowledge serving alias `knowledge__serving` → `knowledge` @2041 (unchanged data;
  code path only)
- Crash re-run 8/8 PASS

### 1.5 Evidence index

| Artifact | Path | Relevant marker |
|---|---|---|
| Ticket state (cutover + rule) | `tasks/micro-tickets-state.json` L920-926 | T-V1-081 note |
| Stale /ready probe | `tasks/micro-tickets-state.json` L739-744 | T-V1-056 |
| Worker restart record | `tasks/micro-tickets-state.json` L753-758 | T-V1-058 |
| Crash re-run | `tasks/micro-tickets-state.json` L900-905 | T-V1-080 |
| Cutover deploy record | `handoffs/RELEASE-RECORD.md` | §Candidate + §Smoke |
| Coordinator-owned deploy gate | `logs/events.jsonl` L1240-1252 | coordinator_update |
| Merge-authority guard | `V4-W0-W1-HANDOFF.md` §1; `COORDINATOR-NEXT-TASK-SPECS.md` §4 | w4:p17 only; "No agent may ... merge, or deploy" |
| Pipeline path | `apps/worker/jobs/ingest_pipeline.py` | T-V1-055/058/080 |
| Purge/dedup path | `packages/ingestion/dedup.py` | T-V1-055 |

---

## 2. Pipeline-restart policy

> **Scope:** `apps/<worker>|packages/ingestion|<pipeline commit>` only.
> **Authority:** coordinator-owned deploys only.
> **Verdict:** never trust `/ready` alone — always add a heartbeat check after restart.

### 2.1 Rule (authoritative)

> **Any commit to `apps/worker`, `packages/ingestion`, or any pipeline code**
> requires **worker restart + `/ready` all-green + heartbeat check** before the
> deploy is considered complete. Deploys are **coordinator-owned only** — no agent
> may self-deploy or mark the ticket DONE without the coordinator merge authority
> (w4:p17).

Path scope (matching the commit trigger):

| Commit path prefix (any file) | Action |
|---|---|
| `apps/worker/**` | → worker restart |
| `packages/ingestion/**` | → worker restart |
| `apps/worker/jobs/ingest_pipeline.py` (the pipeline) | → worker restart |
| *(anything else — e.g. `packages/retrieval`, `packages/answer`, `apps/api`)* | → no auto-restart required by this rule, but a deploy that touches retrieval/answer surfaces must still pass the full gate in §2.3 |

### 2.2 The three-step post-commit gate

Every qualifying commit must complete **all three** steps, in order, before the
deploy ticket moves to DONE. The coordinator owns the final go/no-go.

1. **Worker restart**
   - Cycle the ingestion worker process (graceful stop, then start).
   - Record the new PID and the commit SHA the process is running.
   - Reason: a worker PID can hold stale pipeline code in memory after a purge /
     dedup / delete-fence change — exactly the 06:42Z defect this policy prevents.

2. **`/ready` all-green**
   - Probe `GET /ready` and require **TRUE** (not merely reachable).
   - First-sample false under load is acceptable **only if** the recheck is GREEN
     (mirrors RELEASE-RECORD.md: PG 1212 ms vs 1500 ms budget — transient).
   - If `/ready` is FALSE after restart, roll back the worker to the last-known
     GREEN commit and reopen the ticket.

3. **Heartbeat check**
   - Confirm the worker heartbeat is stable (target 0.8 s; no drift > 2×).
   - Confirm no crash-recovery failures on the restarted PID
     (T-V1-021 / T-V1-080: "crash re-run 8/8").

### 2.3 Deploy authority (coordinator-owned only)

- **Only the coordinator** (museR / Antigravity, merge authority w4:p17) may cut and
  push a deploy that crosses the pipeline-commit boundary. This is enforced by
  `COORDINATOR-NEXT-TASK-SPECS.md` §4 ("No agent may ... merge, or deploy") and the
  review pipeline recorded in `logs/events.jsonl` L1240-1252 (dev11→rew1, dev21→rew2,
  final merge gated on coordinator).
- Agent handoffs that touch the trigger paths must include the post-commit gate
  output (restart PID, `/ready TRUE`, heartbeat ms) and a sign-off line for the
  coordinator.
- If a coordinator-owned deploy is blocked on a pending finding (e.g. V1 Q8/Q9,
  G4/G5, staging outage-injection), the deploy does **not** proceed; the cutover
  record here is updated with the blocker and the deferred date.

### 2.4 When this rule does NOT fire (no-op commits / docs)

This policy is intentionally narrow to keep deploys cheap while closing the
staleness class. It does not apply to:

- Documentation-only commits (`*.md`, `*.rst`, `docs/**`).
- Test-only commits (`tests/**`, `*.log`) that do not touch `apps/worker`,
  `packages/ingestion`, or the pipeline.
- Non-ingestion API changes (`packages/retrieval/**`, `packages/answer/**`,
  `packages/models/**`) that do not alter the running worker binary.

Such commits still require the standard release gate (V1-COMPLETION-PLAN.md §5) but
are exempt from the automatic worker-restart requirement.

### 2.5 Verification checklist (per deploy)

Before closing a deploy ticket that touches a trigger path:

- [ ] Commit SHA is in `apps/worker`, `packages/ingestion`, or the pipeline path.
- [ ] Worker process restarted (PID recorded).
- [ ] `GET /ready` → TRUE on recheck.
- [ ] Worker heartbeat stable (≤ 1.6 s) and crash-recovered (re-run 8/8).
- [ ] Coordinator merge authority (w4:p17) signed off.
- [ ] `handoffs/RELEASE-RECORD.md` updated with the new HEAD digest.

---

## 3. Relationship to other records

- **RELEASE-RECORD.md (T-R03):** this cutover record is the *complement* of the
  release record. RELEASE-RECORD captures commit digests + config smoke; this
  record captures the *worker-restart cutover* and the *restart policy* that was
  missing from the V1 release and is now established.
- **V1-COMPLETION-PLAN.md §5 (release gates):** the restart policy is a concrete
  implementation of the "no false-ready" gate ("ready only after verified").
- **T-V1-056 (stale /ready probe):** this policy's `/ready` all-green recheck is the
  runtime enforcement of the probe correctness fix from T-V1-056.
- **T-V1-021 / T-V1-080 (crash recovery):** the heartbeat check ties deploy success
  to the disposable-services crash-recovery contract.
