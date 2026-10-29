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
git checkout cleanup/portable-install
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

### Optional: rerank sidecar

The `rerank` service is off by default, and nothing in the API or the worker calls it. To start it anyway: `docker compose --profile rerank up -d`.

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

Last run on Windows 11, Python 3.12.13, without Docker or any service running: 2220 passed, 26 skipped, 18 failed, 3 errors. The skips need a dataset that is not in the repo (MuSiQue, 2WikiMultiHopQA, HotpotQA) or a POSIX system. The 21 failures, by cause:

- Postgres (6 failed, 3 errors): `test_pg_fts_lifecycle_gate.py`, `test_lexical_regression.py`, and the purge test in `test_graph_lifecycle.py`.
- Redis (11 failed): the nine tests in `test_queue_semantics.py`, `test_ingest_pipeline.py::test_worker_survives_process_one_failure`, and `test_supersede_then_crash_leaves_no_orphans` in `test_supersession_vector_cleanup.py`. Without Redis the retry is not queued, so the job is marked `SUPERSESSION_CLEANUP_FAILED` and the worker loops time out.
- Stale test data (1 failed): `test_v4_authoring_api.py::test_run_workflow_runtime_failure_returns_coded_error` uses the model `x`, which the admission check rejects before the LLM call. It needs an admitted model and a stubbed provider.

Integration tests need the running stack: `pytest -m integration`.

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
