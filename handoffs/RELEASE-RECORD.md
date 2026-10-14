# Release record — T-R03 (PRIO1 ops baseline)

Date: 2026-10-14. Owner: museR. Review gate: reviews/2026-10-14-super-rag-v1-v4-review/.

## Candidate
- Tree HEAD: `b190dfc` (R06b). No product change since R06b except this record.
- Images (digests, `docker images`): knowledge-os-api `e2e9bbec`, worker `60cbb9f4`,
  web `2dce58df`, qdrant v1.19.2 `b7b0444c`, redis 7.4.11 `858f009f`,
  postgres 16.15 `621a7610`, infinity 0.0.77 `11e8b392` (pinned; `:latest` identical digest, unused).
- Config: compose pins VECTOR_COLLECTION (fail-closed), AUTH_JWT_SECRET `:?`
  (fail-closed), GRAPH_MODE off, GRAPH_DB_PATH absolute + graphdata volume.
  Enabled features: V1 full (upload-only; connectors continuous sync M6-pending;
  conservative cross-language verification path active), V2 flag-off, V3 API-only
  (no worker), V4 inert (all flags off), graph consumer wired-not-enabled.

## Smoke (this record)
- /health 200. /ready TRUE on recheck (first sample false: PG 1212ms vs 1500ms
  probe budget under load — known transient class, all 7 deps reachable).
- bge-m3 1024-d live. Reranker ranks (2 scores). Worker heartbeat 0.8s.
- Live knowledge: 2041 points. Alias knowledge__serving -> knowledge @2041.

## Sequenced, NOT done here
- /version still reports git_sha unknown: wiring it needs an api code change,
  and ANY api reload/restart runs the R01 boot refusal (live env still holds
  the old default). Sequence: export AUTH_JWT_SECRET (>=32ch) -> controlled
  restart -> verify /version + login/refresh -> rotate per R01 runbook.
- Continuous connector sync (T-R09): ship V1 as upload-only with M6-pending.
  QueuedSyncConsumer and progress store remain library-complete and unit-tested in
  packages/connectors; automated background worker sync, workspace connector routes,
  and live connector scheduling are deferred to M6.
- Staging outage-injection: no staging exists; deferred until one does.
- Immutable images / no source mounts: larger migration, maintenance window.
