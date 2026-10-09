# Knowledge OS

Self-hosted, local-first AI knowledge platform. This repository is the M0
skeleton: a runnable stack with stable adapter boundaries for the PD-001/PD-002
selected components. **No real parsing, embedding, reranking or LLM wiring yet**
— interfaces and stubs only.

> **OPERATIVE PLAN & CONTROLLING SPECIFICATION:**
> The authoritative plan adopted by the team on October 7–8, 2026 is defined in:
> - [`OPERATIVE_PLAN.md`](./OPERATIVE_PLAN.md)
> - [`../rag-fork-solution/PLAN.md`](../rag-fork-solution/PLAN.md) & [`../rag-fork-solution/TASKS.md`](../rag-fork-solution/TASKS.md)
> All agents (boss, coders, reviewers, testers) must strictly consult `rag-fork-solution/` for milestone and task definitions.

Reuse-first: see `../knowledge-os-reuse-first-starter/docs/reuse/REUSE_STRATEGY.md` and `../rag-fork-solution/REUSE_MAP.md`.

## Repository layout

```text
apps/
  api/        FastAPI service: /health, /ready, /version, Alembic, config
  worker/     stub worker: boots, connects Redis, "no jobs registered (M0)"
  web/        Next.js shell (owned by code2)
packages/
  core/       config, structured logging, auth/ACL stub types, health types
  ingestion/  interfaces.py  -> DocumentParser, DocumentElement   (PD-001)
  retrieval/  interfaces.py  -> VectorStore, LexicalStore, EmbeddingProvider,
                                Reranker, RetrievalOrchestrator    (PD-002)
  models/     interfaces.py   -> LLMProvider                      (M4)
  connectors/ interfaces.py   -> Connector                        (later)
infrastructure/docker/   image pins and operational notes
tests/                   unit + boot integration tests
```

Vendor SDKs may only be imported inside `*/adapters/*`. Interface modules stay
vendor-free (stdlib + pydantic). This is enforced by review and CI lint rules.

## Prerequisites

- Docker + Docker Compose v2 (`docker compose version`)
- Python 3.12+ (for running the backend or tests without Docker)
- Node 20+ (for the frontend; see `apps/web/README.md`)

## Quickstart

```bash
cp .env.example .env
docker compose up -d --build      # or: make up
docker compose ps
```

Services and ports:

| Service  | URL / port | Notes |
|---|---|---|
| web      | http://localhost:3001 | Next.js status page calling `/health` |
| api      | http://localhost:8000 | `GET /health`, `/ready`, `/version` |
| postgres | localhost:5432 | metadata + lexical FTS |
| redis    | localhost:6379 | worker broker |
| qdrant   | http://localhost:6333 | dense vectors (`/readyz`) |
| minio    | http://localhost:9000 (API), http://localhost:9001 (console) | document originals |

Health checks:

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8000/ready    # 200 always; per-service reachability
curl -s http://localhost:8000/version
```

`/ready` is intentionally non-fatal: it returns HTTP 200 with
`status: "ok" | "degraded"` and a `dependencies` map. The UI renders an explicit
degraded/error state instead of the API refusing to start.

## Configuration

All settings are environment-driven (see `.env.example`). Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `local` | Runtime environment label |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | Structured logging |
| `DATABASE_URL` | `postgresql+psycopg://knowledge:knowledge@localhost:5432/knowledge` | Postgres |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant HTTP |
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | local defaults | S3 originals |
| `MIGRATIONS_ON_BOOT` | `true` | Run `alembic upgrade head` at API start (non-fatal) |
| `CORS_ORIGINS` | `http://localhost:3001` | Comma-separated allowed origins |
| `MINIO_IMAGE` | pinned MinIO repackage | See monitoring note below |

## Database migrations

The M0 baseline (`apps/api/alembic/versions/0001_baseline.py`) is intentionally
empty — no domain tables until M1 (`docs/architecture/data-model.md`). The API
applies migrations on boot; to run them manually:

```bash
make migrate
make revision m="add workspaces"
```

## Where adapters will live

Each selected third-party component is isolated behind an interface and will be
implemented under an `adapters/` package:

| Component (decision) | Interface | Future adapter location |
|---|---|---|
| pypdf / pdfplumber / python-docx / bs4 / markdown-it-py (PD-001) | `ingestion.interfaces.DocumentParser` | `packages/ingestion/adapters/` |
| Qdrant (PD-002) | `retrieval.interfaces.VectorStore` | `packages/retrieval/adapters/` |
| PostgreSQL FTS (PD-002) | `retrieval.interfaces.LexicalStore` | `packages/retrieval/adapters/` |
| bge-m3 / OpenAI embeddings (PD-002) | `retrieval.interfaces.EmbeddingProvider` | `packages/retrieval/adapters/` |
| bge-reranker-v2-m3 (PD-002) | `retrieval.interfaces.Reranker` | `packages/retrieval/adapters/` |
| OpenAI-compatible / vLLM / Ollama (PD-002) | `models.interfaces.LLMProvider` | `packages/models/adapters/` |
| web / S3 / Git connectors | `connectors.interfaces.Connector` | `packages/connectors/adapters/` |

## Frontend

Owned by code2 under `apps/web/**` (Next.js + TypeScript strict + Tailwind).
The `web` compose service builds the app's production Dockerfile (standalone
output) and proxies `GET /api/health` to the backend server-side via the
`API_BASE_URL` env var (set to `http://api:8000` in compose), so the browser
stays same-origin and no CORS coupling is needed. See `apps/web/README.md`.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

make api-dev       # local uvicorn --reload
make worker-dev    # local worker stub
make lint          # ruff
make typecheck     # mypy
make test          # pytest (unit + boot integration)
```

Tests are split by marker: `pytest -m unit` needs no services; `pytest -m
integration` runs the boot checks and skips dependency assertions with a clear
message when compose is not up.

## Monitoring / known operational note

MinIO's official Docker images were withdrawn from Docker Hub in 2026 and Quay
now requires authentication for anonymous pulls (MinIO also reached EOL). The
skeleton pins a MinIO **repackage** by digest so the stack boots, overridable
via `MINIO_IMAGE`. This needs a component decision before v1; see
`infrastructure/docker/README.md`.
