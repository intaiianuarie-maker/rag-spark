# Knowledge OS

Self-hosted, local-first AI knowledge platform.

> **OPERATIVE PLAN & CONTROLLING SPECIFICATION:**
> [`OPERATIVE_PLAN.md`](./OPERATIVE_PLAN.md) and the shared `../rag-fork-solution/` plan docs.

## Quickstart (local stack)

```bash
make up       # build + start full stack
make ps       # service status
make logs     # follow logs
make down     # stop (keeps volumes)
```

Migrations (host, postgres running):

```bash
make migrate
```

Backend dev loop:

```bash
make api-dev
make worker-dev
```

Tests:

```bash
make test        # full suite
make test-unit   # unit only
make test-integration
```

Lint / typecheck:

```bash
make lint
make typecheck
```

## Repository layout

- `apps/api` FastAPI service (workflows, search, health)
- `apps/worker` agent runtime worker
- `apps/web` Next.js UI
- `packages/` core, ingestion, retrieval, models, workflow_engine, agents
- `infrastructure/docker` image pins and ops notes
- `tests/` unit + boot integration

## Feature maturity

| Area | Status | Subsystem |
|------|--------|-----------|
| Dense + lexical hybrid retrieval (RRF) | Implemented | `packages/retrieval/` |
| Workflows (draft/validate/approve/run, checkpointing) | Implemented | `apps/api/app/workflows/`, `packages/workflow_engine/` |
| V3 agent adapter | Implemented (flag-gated, off by default) | `packages/workflow_engine/adapters/v3_adapter.py` |
| Rerank leg | Implemented behind `RERANK_ENABLED` (default off) | `packages/rerank/`, factory seam |
| Graph retrieval leg | Construction-gated; degrades to off | `graph/`, factory |
| Oral/connector surfaces | Planned / experimental | connectors interfaces |
| V4 UI authoring | Implemented surface + validators | `apps/web` |

## Architecture (one-pager)

ACL scope is enforced before fan-out; dense/lexical (and optional graph) results
fuse via deterministic RRF; rerank (optional, cloud, gated) then context
selection; provenance/trace recorded per item. Workflow runs checkpoint at
boundaries and resume via idempotency ledger. See `handoffs/CODE2-HANDOFF.md`
and `reviews/` for decision record.

## Status notes

flags and degrade explicitly rather than fake success.
