# Knowledge OS

Self-hosted knowledge platform. Upload documents, search them (dense and keyword hybrid), and ask questions that get answers with citations. It runs as a Docker Compose stack on your own machine.

## Before you start

- **Docker.** Docker Desktop on Windows or macOS, or Docker Engine with the Compose plugin on Linux. Start it and wait until it reports that it is running.
- **Git.**
- **An OpenRouter API key** from https://openrouter.ai/keys. Search and answers need it.

## Install and start

```bash
git clone https://github.com/intaiianuarie-maker/rag-spark.git
cd rag-spark
cp .env.example .env
```

On Windows PowerShell, use `Copy-Item .env.example .env` instead of `cp`.

Open `.env` and set two values:

- `OPENROUTER_API_KEY`: your OpenRouter key.
- `AUTH_JWT_SECRET`: a random string of at least 32 characters. Generate one with Docker, so you don't need Python on the host:

```bash
docker run --rm python:3.12-slim python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Start the stack:

```bash
docker compose up -d --build
docker compose ps
```

The first build downloads images and installs dependencies, so it takes a while. Then open:

- **App:** http://localhost:3001. Create an account on the sign-up page.
- **API health:** http://localhost:8000/health, and readiness at http://localhost:8000/ready.

Stop the stack with `docker compose down`. Your data stays in Docker volumes. `docker compose down -v` deletes all data.

Ports are bound to `127.0.0.1`, so only this machine can reach them. To reach the stack from another device, change the `127.0.0.1:` prefix in `docker-compose.yml` and read the security notes below first.

The api and worker images contain the code as of the build; nothing from your checkout is mounted into them. After `git pull`, run `docker compose up -d --build` again to pick up the changes.

### Contributor mode (live reload)

To edit code and see it without rebuilding, add the dev override. It bind-mounts `apps/` and `packages/` into the api and worker and runs the api with `--reload`:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

The override sets `--reload-dir` for both `apps/api` and `packages/`; uvicorn watches only the working directory by default, so without it edits under `packages/` are silently ignored. The worker has no reload watcher: restart it after editing (`docker compose -f docker-compose.yml -f docker-compose.dev.yml restart worker`). Don't use this mode for a normal install, because it runs whatever is on your disk.

### Optional: rerank sidecar

OpenRouter Nemotron rerank is on by default for the ask leg (`RERANK_ENABLED=true`); nothing extra needs to run. The standalone `rerank` service (a local MiniLM sidecar) is off by default and is not called by the API or worker — start it only if you want the local route: `docker compose --profile rerank up -d`.

## Configuration

`.env.example` lists the settings you may need. Only `AUTH_JWT_SECRET` and `OPENROUTER_API_KEY` are required. Ports (`WEB_PORT`, `API_PORT`) and `CORS_ORIGINS` have defaults. The knowledge graph is off by default (`GRAPH_MODE=off`).

Fixed in code, not in `.env`: embeddings use `google/gemini-embedding-2` through OpenRouter, with 3072 dimensions.

## Tests

Use Python 3.12, the version the Docker images use:

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -m unit
```

The unit and contract tests need a Postgres and a Redis, as CI provides them (`postgres:16.15-alpine3.23` and `redis:7.4.11-alpine`, the images in `docker-compose.yml`). Start your own, apply the migrations, and point the tests at them. Don't point them at a running stack you care about:

```bash
docker run -d --name test-pg -e POSTGRES_USER=knowledge -e POSTGRES_PASSWORD=knowledge -e POSTGRES_DB=knowledge -p 127.0.0.1:55432:5432 postgres:16.15-alpine3.23
docker run -d --name test-redis -p 127.0.0.1:56379:6379 redis:7.4.11-alpine
export DATABASE_URL=postgresql+psycopg://knowledge:knowledge@127.0.0.1:55432/knowledge
export REDIS_URL=redis://127.0.0.1:56379/0
(cd apps/api && PYTHONPATH=../../packages alembic upgrade head)
pytest -q --ignore=tests/integration
```

Last run on Windows 11, Python 3.12, against throwaway Postgres 16 and Redis 7 containers: 3485 passed, 35 skipped, 0 failed. Skips are conditional on the environment:

- Datasets that are not in the repo: MuSiQue, 2WikiMultiHopQA, HotpotQA, `/tmp/g5_mappings.json`.
- POSIX-only tests, and one `file://` containment test on Windows (the product runs in Linux containers).
- The end-to-end OCR tests need the `tesseract` binary with the `ron`, `por` and `eng` packs, plus the DejaVu font. CI installs them (`apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-ron tesseract-ocr-por fonts-dejavu-core`); elsewhere they skip.
- `test_live_stability` pins one operator's live Qdrant index; set `G5_LIVE_QDRANT_CHECK=1` to run it.

Without Postgres or Redis, the tests that use them fail instead of skipping: `test_pg_fts_lifecycle_gate.py`, `test_lexical_regression.py`, `test_queue_semantics.py` and a few others.

Integration tests need the running stack, and CI skips them: `pytest -m integration`.

Lint and types: `ruff check .` and `mypy`.

If you have `make`, the same tasks are available as `make up`, `make down`, `make test-unit`, and so on. Run `make help` for the list.

## Repository layout

- `apps/api`: FastAPI service. Auth, workspaces, documents, search, ask, workflows. Runs database migrations on start.
- `apps/worker`: ingestion worker. Parses, chunks, embeds and indexes documents.
- `apps/web`: Next.js UI.
- `apps/rerank`: optional rerank sidecar, not used by default.
- `packages/`: shared code. Config, storage, retrieval, answer engine, models, workflow engine.
- `tests/`: unit, contract and integration tests.
- `evals/`: benchmark harness and its results (`evals/benchmark_v1/results`).
- `docs/`: design, ingestion and evaluation notes.
- `scripts/`: operational and guard scripts.

## Known limits

- **Sign-up is open.** Anyone who can reach the web port can create an account. Keep the stack on localhost, or gate sign-up before you expose it.
- **No TLS.** The compose file serves plain HTTP. Put a TLS reverse proxy in front of it for any shared use.
- **Slow answers.** On the review server, median ask latency was 28 to 42 s and p95 was 61 to 127 s. That is not measured on your machine.
- **Chunk IDs can appear in answers.** Not fixed.
- **`include_public` in the ACL code is not what its name suggests.** Not fixed. Review it before you rely on it for access control.
- **Web connector SSRF guard is unit-tested only.** The sync worker is disabled in the compose stack, so the guard has not run live.

## Security notes

- Backing services (Postgres, Redis, Qdrant, SeaweedFS) are reachable only from this machine. Their default credentials (`knowledge`, `admin`) are for local use. Change them before any shared use.
- `.env` holds your secrets. It is gitignored. Never commit it.

## Repo notes

Agent handoff and review notes were removed from the working tree. They remain in git history, up to commit `c5380f2`.
