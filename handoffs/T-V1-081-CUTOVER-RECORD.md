# T-V1-081 — V1 deploy cutover record + pipeline-restart policy

Date: 2026-10-16 (cutover observed 06:42Z). Coordinator-authored from ticket evidence (dev1 analysis assisted).

## Cutover record

Worker pipeline code for T-V1-055 / T-V1-058 / T-V1-080 went stale at ~06:42Z:
the running worker process was serving pre-commit code after these commits
landed, so the /ready probe reported stale behavior.

- T-V1-055 (dev1, DONE): `33d3c09` + harness `b6a8916` — delete originals from
  object storage on purge.
- T-V1-058 (DONE): `66d78bb` — originals-purger ordering bug (self-inflicted by
  T-055) fixed; **live worker restarted** (this is the cutover).
- T-V1-080 (dev1, DONE): `6227b8d` — delete-fence precedence both directions
  green; crash re-run 8/8 by museR.

Resolution: worker restart picked up the new pipeline code; /ready returned
all-green and heartbeat confirmed. No data loss; stale window bounded by the
06:42Z detection.

## Pipeline-restart policy (standing rule)

1. Any commit touching `apps/worker`, `packages/ingestion`, or pipeline code
   REQUIRES: worker restart + `/ready` all-green + heartbeat check before the
   deploy is considered complete.
2. Coordinator-owned deploys only — no direct prod pushes from dev panes.
3. Staleness signal (stale /ready, heartbeat gap, version mismatch) triggers an
   immediate restart + re-verify, recorded here with timestamp.

## Verification

- Ticket states: T-V1-055/058/080 all DONE in micro-tickets-state.json.
- Handoff rows: CODE1-HANDOFF.md (dev1 commits 33d3c09, 6227b8d), T-058 note
  records the live worker restart.
- No product code touched by this record.
