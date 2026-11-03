# Handoff: continue development and tests

Audience: an LLM agent taking over this work with no prior context. Read this file fully before touching anything.

Status date: 2026-11-03. Repo: `C:\Users\ciprian.stoica\Antigravity\rag-spark-portable`.

## 1. Where things stand

- Branch `cleanup/portable-install`, HEAD `6a2f5f2`. **Nothing from the recent rounds is committed.** `git status` shows about 93 paths: 74 modified tracked files and 19 untracked (new modules, new tests, `docker-compose.dev.yml`, `.dockerignore`, `packages/models/credentials.py`, `packages/retrieval/multimodal_leg.py`, and two docs under `docs/`).
- Behaviour already delivered in the tree (not yet proven in CI): baked images with no source mounts, a dev override file, the `docling_hybrid` chunker, vision and multimodal steps (flag-gated, off by default), the graph ask leg, Stage 11/12 hardening, host-bound API keys, durable agent steps and output, upload MIME resolution, failure cleanup with an ownership check, and mypy and vendor-import guard cleanup.
- Remediation plan: `docs/REMEDIATION-PLAN-ACCEPTANCE-REVIEW.md`. Read it. It defines tickets A1 to A6, the bug IDs B1 to B18, the default decisions, and the "done when" line per ticket.

### Latest status of the bug list (from the last reproduction pass)

| ID | Status | Ticket |
|---|---|---|
| B1 | Fixed on production paths. The sync `chat()` helper in `openrouter_provider.py` (~1200-1218) still retries 401 into paid, but nothing calls it. Delete or guard it. | A1 |
| B2 | Reproduces. OUTPUT publishes upstream output when `verified` is false (`production_executor.py` ~115-117). Router builds the executor with no verifier (`workflows/router.py` ~674). | A3 |
| B3 | Reproduces. Research is inline, stored in `_RESEARCH` dict (`agents/research.py` ~36), lost on restart. | A4 |
| B4 | Reproduces. `completion_tokens=0` (`agents/runtime.py` ~1338). | A4 |
| B5 | Reproduces. Agent and research answers have no sources and skip Stage 11/12. | A4 |
| B6 | Reproduces. AGENT node validates, then fails at run (`router.py` ~674-680 never calls `configure_agent_executor`). | A3 |
| B7 | Reproduces. Ask reads only the env var (`ask/router.py` ~170, ~268 call `load_workspace_config(ws)` without a session). `router_config.py` ~377 uses sync `session.execute`. A sensitive workspace still makes cloud calls. **Highest priority.** | A2 |
| B8 | Fixed (`semantic_verification.py` ~588 now uses fixed text). | A5 |
| B9 | Reproduces. Document reports ready before graph extraction. | A6 |
| B10 | Fixed (`engine.py` ~668-692 abstains on Stage 11 exception). | A5 |
| B11 | Reproduces. `credential_hygiene.py` ~78 (`_id` suffix) and ~115. Validate says valid, save returns 422. | A3 |
| B12 | Reproduces. `pg_fts.py` ~211-215 uses `websearch_to_tsquery` (AND semantics). | A6 |
| B13 | Reproduces. No UNRESPONSIVE verdict (`gemma_semantic_verifier.py` ~238-244, verify_claim ~160 never gets the question). The "B13 abstains" default is not implemented. | A5 |
| B14 | Reproduces. Planner `json.loads` on fenced output (`planner.py` ~335). | A4 |
| B15 | Reproduces. `verifier_model_id = self.model` (`engine.py` ~825). Reports the requested `:free` slug. | A1 |
| B16 | Half fixed. `/ready` 404 handling is fixed (`readiness.py` ~196-198). Rerank disclosure still says "local reranker deleted per user order" (`orchestrator.py` ~89-91, ~981-984). | A6 |
| B17 | Reproduces. No parse retry (`engine.py` ~597-630). | A5 |
| B18 | Reproduces. `provider.only` and `data_collection` are not sent on ask payloads (`openrouter_provider.py` ~559-563, `engine.py` ~572-580). | A1 |

Ticket status: none meets its "done when" line in full. A1 and A5 are partial.

### New findings not yet ticketed

- `/ready` reports `ready: false` because OpenRouter returns 404 on `/health/liveliness`. Layer status then shows agents as disabled, even though agent runs work. Add to A6.
- A cancelled workflow's LLM node still runs to `completed` after the cancel. Add to A3.
- Checkpoint resume: no resume endpoint exists. Unverified. Decide whether it is in scope.

### Open decisions (need the user)

1. `REMEDIATION_REPORT.md` is an untracked file in the repo root. It was not written by the planned work and describes a different project. **Decision: keep it.** Do not delete it. Do not commit it unless the user asks for that specifically.
2. Commit policy: **no commits until the user says so.** When asked, the commit should be split by ticket, not one large commit.
3. Default decisions in the plan (B13 abstains, AGENT nodes wired not removed, research in the sqlite agent store). Follow them unless the user changes them.

## 2. Rules for the LLM doing the work

- **Do not** run `git stash`, `git reset`, `git checkout` (of files), `git clean`, or any command that discards uncommitted work. The tree is the only copy of the recent work.
- **Do not commit or push** unless the user explicitly asks.
- **Do not touch the shared dev stack**: compose project `knowledge-os` on ports 8000, 3001, 5432, 6379, 6333, 8333 and neighbours. Never restart, rebuild, or remove it.
- Live tests use your own compose project (for example `ko-<name>`) with every published host port moved to a free random port. Tear it down at the end: `docker compose -p <project> down -v --rmi local`.
- **Secrets:** read `OPENROUTER_API_KEY` in-process from the repo `.env`. Write it only into a scratch env file. Never echo it, log it, or put it in a report. Delete the scratch env file at the end.
- **Paid calls:** OpenRouter free Gemma first. Cap a live pass at about 150 billed calls. Report your estimate.
- **Datasets:** public Hugging Face samples only, small, in their own folder, parsed as data only. Kaggle needs credentials: do not use it.
- **Ownership:** one agent per file. Do not edit a file another ticket owns. If you must, say so in your report.
- **Failing tests are findings, not noise.** Do not weaken an assertion to make a test pass without naming the change and why.

## 3. Environment

- Python: create a fresh venv (for example `python -m venv <scratch>\venv312` on Python 3.12) and install the dependencies listed in `.github/workflows/ci.yml` (backend job). The earlier venv lives in the session scratchpad and may not exist for you.
- Windows: set `PYTHONPATH="packages;apps/api;apps/worker"` (semicolons, not colons).
- Unit and contract tests, with every service dead:
  - `DATABASE_URL=postgresql+psycopg://u:p@127.0.0.1:1/db?connect_timeout=1`
  - `REDIS_URL=redis://127.0.0.1:1/0`
  - `API_BASE_URL`, `WEB_BASE_URL`, `QDRANT_URL` = `http://127.0.0.1:1`
  - `MINIO_ENDPOINT`, `S3_ENDPOINT_URL` = `http://127.0.0.1:1`
  - Empty `OPENROUTER_API_KEY`
  - Run: `pytest -q --ignore=tests/integration`
- Integration tests: `pytest -q tests/integration` with the same dead URLs. Expected: 0 failed, 0 errors (skips are expected).
- Worker Dockerfile: the apt step gets 403 over plain HTTP on this network. Build from a scratch copy of the Dockerfile that installs `ca-certificates` first and switches apt sources to https. Do not edit the repo Dockerfile for this.
- Lint and types: `ruff check .` (clean). `ruff format --check` shows about 47 pre-existing drifted files: do not reformat files you did not create. `mypy` (pyproject config) was clean at 264 files. Vendor-import guard: must find no hits outside `/adapters/`.

### Baseline numbers to compare against

- Unit (dead URLs): 3577 passed, 23 failed, 36 skipped, 6 errors. Failures are DB/Redis-dependent (`test_pg_fts_lifecycle_gate`, `test_lexical_regression`, `test_queue_semantics`, one ingest worker test), plus Windows-only docling path-length issues.
- Integration (dead URLs): 30 passed, 180 skipped, 2 xfailed, 0 failed.

## 4. Work to do, in order

### Wave A (parallel, separate files)

**Start with A2 alone.** Do not start the other Wave A tickets until A2 is finished and its tests pass. A2 is the sensitive-workspace egress leak (B7), the highest-risk bug, and it touches the ask path that A5 also changes. Finish A2 first, then run A1, A3, A5 and A6 in parallel.

Each ticket: write a failing test first, make it pass, add a failure-path test, check the "done when" line in the plan.

- **A2 (Opus medium, highest risk, do first):** B7. `load_workspace_config` must read the DB row. `sensitive=true` forces the local route or refusal, with zero cloud calls. Prove with a spy transport. Files: `apps/api/app/ask/router.py`, `packages/models/router_config.py`, model-config store.
- **A1 (Sonnet high):** fallback policy B1 (close the sync helper), B15 (served model slug, `engine.py` ~825 and `openrouter_provider.py` ~696-698), B18 (`provider.only` and data collection on generation payloads). File: `packages/models/adapters/openrouter_provider.py` and the engine call site.
- **A3 (Sonnet high):** B2 (OUTPUT fails when `verified` is false), B6 (wire `configure_agent_executor` or drop AGENT from the manifest, decide after reading the code), B11 (exempt UUID `chunk_id` from credential check; validate runs the same check), cancelled LLM node still completing. Files: `production_executor.py`, `apps/api/app/workflows/router.py`, `credential_hygiene.py`.
- **A5 (Opus medium):** B13 (UNRESPONSIVE verdict; abstain), B17 (one parse retry). Files: `packages/answer/engine.py`, `semantic_verification.py`, `gemma_semantic_verifier.py`. Check B8 and B10 stay fixed.
- **A6 (Sonnet high):** B12 (OR semantics for natural-language questions, `pg_fts.py`), B9 (per-document graph status, "graph pending" in the trace), B16 (rerank disclosure text), `/ready` liveliness 404 false negative.

### Wave B (after A5)

- **A4 (Sonnet high):** B3 (research persisted in the sqlite agent store and queued like `/runs/queued`), B4 (real completion tokens), B5 (agent and research answers go through the A5 Stage 11/12 path), B14 (planner strips code fences).

### Wave C (proof)

1. Run every touched test file, then `ruff check`, `mypy`, the vendor guard, and the dead-URL unit and integration runs. Compare with the baseline above. No new failures.
2. Live replay on a fresh `ko-<name>` stack: the B7 sensitive-workspace egress test, the V4 OUTPUT and AGENT cases, research after an api restart, the SQuAD unanswerables, and one scanned PDF for vision. Keep it to about 20 asks.
3. Re-score the feature table in the acceptance review with the same method.

### Not yet covered (add only if the user wants them)

- Checkpoint resume of a crashed workflow run.
- GitHub CI run: impossible until the branch is pushed. Say so in the report.
- Connector sync for web, git and local sources was verified live once; deletion propagation was verified once.

## 5. Definition of done (per ticket and overall)

- Each ticket's "done when" line in the plan is met, with a test that fails before the change and passes after.
- No new failures in the dead-URL unit run beyond the baseline list.
- `ruff check`, `mypy` and the vendor guard are clean.
- The live replay shows the fixed behaviour on the running stack, and the stack is torn down.
- A short report per ticket: files changed, tests added, what was not verified.
- Nothing committed unless the user asked. Note in the report which files each ticket touched so commits can be split.

## 6. Report format for each agent

- Ticket and bug IDs covered.
- Files changed, with a one-line reason each.
- Tests added or changed, with the names and any assertion you changed, and why.
- Commands run and their exact result counts.
- Anything not verified, and why.
- Any file you saw changing that you do not own (possible concurrent writer).

## 7. Things to watch

- Other agents may write into the repo concurrently. Re-read a file before editing it.
- Windows temp-dir cleanup warnings (`rm_rf WinError 145`) are noise, not failures.
- The live stack wipes container logs when containers are recreated, so count paid calls as you go.
