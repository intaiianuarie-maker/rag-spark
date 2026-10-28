# Super RAG review — 2026-10-17

Reviewed the `knowledge-os` project at `f5675d45d6bb7e4fad7a7cc6e9876e179f7338d5`, including its existing local changes. Scope: earlier review fixes, ingestion/retrieval boundaries, agent persistence and worker delivery, search workspace isolation, and repository quality gates. This is a general code review, not a comprehensive audit or product acceptance. No application code was changed.

## Findings

### 1. [P1] Default deployment still accepts agent jobs into process-local storage

`docker-compose.yml:137` defaults `AGENT_STORE_DB` to empty. API startup (`apps/api/app/main.py:61`) and worker startup (`apps/worker/worker/main.py:49`) only install the new shared SQLite stores when this variable is nonempty. Otherwise `packages/stores/agent_stores.py:406–459` still constructs in-memory run/step/queue stores. The queued-run endpoint persists and enqueues through those accessors and returns 202 (`apps/api/app/agents/lifecycle_routes.py:389–406`) without requiring durable configuration.

The SQLite implementation and both startup hooks are real improvements over the October 15 state, but the default separate API/worker processes still cannot exchange accepted runs. A fresh-process probe enqueued one run in the parent and measured depths **parent 1, child 0**. The evidence store also remains in memory when SQLite is enabled.

Require a shared path under the existing shared volume when queued agents are enabled, or refuse admission with an explicit unavailable response. Validate API admission and consumption across independently started processes, including restart recovery.

### 2. [P1] SQLite queue claim is not atomic across consumers

`packages/stores/agent_stores.py:575–588` selects a ready row before its first write transaction, then deletes it and uses `INSERT OR REPLACE` to install a claim. Its `threading.Lock` belongs to each queue object and cannot serialize independent workers. Two connections can both select the same row; the second delete affects zero rows but is not checked, and its replace overwrites the first consumer's lease token. Both callers receive a successful claim and can begin executing the same run.

Reproduced with two real queue instances sharing one temporary SQLite file, each using its own connection. A connection wrapper places a barrier immediately after the real selection to force the possible interleaving; all SQL and commits still execute against SQLite. Both claims returned **`same-run`**, with different tokens; subsequent acknowledgements returned **`false`, `true`**. This demonstrates a queue contract failure; duplicate external side effects were not exercised.

Acquire the write transaction before selecting (for example, `BEGIN IMMEDIATE`) or use an atomic checked delete/claim operation. Review acknowledgement, renewal, parking, and expired-claim recovery for the same check-then-write pattern. Add an independent-consumer regression test; reopening one queue sequentially does not exercise this race.

### 3. [P2] Search workspace reset does not invalidate an outstanding request

`apps/web/src/app/search/page.tsx:34–38` now clears existing results and selection when the workspace changes, but does not increment `runIdRef`. The generation counter changes only when another search starts (`:46`). Start a slow search in A, switch to B without starting another search, then allow A to finish: its generation still matches at `:57`, so A's results appear under B. The error path has the same issue; the unconditional `finally` at `:65` can also clear loading for a newer request.

Key the search component/state to workspace identity, or invalidate/abort pending work on every workspace change and guard all completion paths. Existing 227 frontend tests pass, but do not establish this workspace-switch behavior. This is a display scoping defect; no backend authorization bypass is claimed.

### 4. [P1] Required repository checks fail on the review snapshot

The current CI workflow cannot be treated as green:

- `ruff check . --statistics`: **29 errors** (26 E501, two E402, one F821). Twenty-three diagnostics come from the three previously untracked BFCL files; six already exist in tracked files. The undefined name is `Any` in `tests/unit/test_local_only_no_egress.py:368`.
- `/root/ko-venv/bin/mypy`: **17 errors in six files**, including agent policy state, vision message typing, Docling adapters, optional multimodal provider access, and graph community provider typing. This is the installed Python 3.14 environment; CI pins Python 3.12, so exact dependency-sensitive diagnostics may differ there.
- `npm run lint`: fails `react-hooks/set-state-in-effect` at `apps/web/src/app/search/page.tsx:35`.
- `bash scripts/model_origin_guard.sh`: fails because `.env.example` contains `DeepSeek` and pins `DEFAULT_MODEL=gpt-4o-mini` outside the script's allowlist. These are guard/configuration inconsistencies, not proof that a prohibited model executed.
- The CI vendor-import predicate also finds imports outside `/adapters/`, including graph Redis imports, ingestion's LlamaIndex availability probe, `packages/ingestion/ocr.py`, and rerank files. The authorization-role predicate found no matches outside `app/authz/`.

Restore the actual configured gates before requesting merge or acceptance. Do not weaken policy checks solely to obtain green output. The GitHub branch is a review snapshot containing existing work, not a deployment approval.

### 5. [P2] Documented first-run configuration cannot start the shipped stack

The README still advertises `make up`, and `.env.example:2` says every setting has a working Compose default. The example neither supplies `VECTOR_COLLECTION` nor any of the four required `WORKFLOW_*_DB` variables, while Compose uses required-variable expansion for all five. Its `AUTH_JWT_SECRET` is also the now-rejected shipped development value.

The earlier workflow wiring defect is fixed: Compose now forwards the four database variables and shares `workflowdata:/app/.workflow` between API and worker. The remaining issue is that following the published setup does not supply that configuration. Document shared volume paths and secret generation, and include an example matching current requirements. No services were started or restarted during this review.

## Earlier findings revisited

- **Workflow persistence:** required variables and shared volume are present; the October 15 omission is superseded by finding 5.
- **Lexical lifecycle:** the real query now includes document/version joins and a lifecycle predicate before ranking (`packages/retrieval/adapters/pg_fts.py:74–97, 220–230`). The previous missing-predicate finding is resolved in source. Live PostgreSQL lifecycle behavior was not re-executed here.
- **Sensitive ingestion:** the embedding stage now checks workspace sensitivity and refuses cloud embedding before constructing the provider (`apps/worker/jobs/ingest_pipeline.py:1042–1049`). A fully functional local embedding route is not established by that refusal.
- **Lost-region disclosure:** ingestion now merges disclosure into canonical, lexical and vector metadata. Focused downstream disclosure tests pass; the mixed-scan ingestion-to-answer path was not executed.
- **Evaluation import collision:** the fixture and driver use explicit path loading. Full collection now succeeds, and the paired graph/Q9 tests pass in the focused run.

## Verification

| Check | Outcome |
|---|---|
| `npm test -- --reporter=dot` | 25 files, **227 passed** |
| `npm run typecheck` | **Passed** |
| Focused backend command below | **147 passed**, one deprecation warning |
| `/root/ko-venv/bin/python -m pytest --collect-only -q` | **3,096 collected**, exit 0, 90.59 seconds |
| Full backend execution | Not run; collection is not execution |
| Queue default/fresh-process probe | Parent 1, child 0 |
| Concurrent SQLite queue probe | Two successful claims for the same run |
| `git diff --check` | Passed before adding this report |

Focused backend invocation (bounded at 150 seconds, completed in 14.59 seconds):

```sh
TMPDIR=/tmp /root/ko-venv/bin/python -m pytest -q \
  tests/unit/test_t_i01b_sqlite_agent.py \
  tests/unit/test_workflow_config_enforcement.py \
  tests/unit/test_graph_paired_eval.py tests/unit/test_q9_gates.py \
  tests/unit/test_agent_lifecycle.py tests/unit/test_local_only_no_egress.py \
  tests/unit/test_answer_lost_region_disclosure.py \
  tests/unit/test_t_k06a_disclosure.py evals/test_bfcl_battery.py
```

The full collection was rerun outside the sandbox after the sandboxed collection stalled; the stalled process was interrupted. Collection's existing session hook reported dropping its own `knowledge_test_security_*` scratch collection. No production service was restarted, no release deployed, and no paid model evaluation invoked.

Logs and the queue reproduction are in [2026-10-17-super-rag-review/](2026-10-17-super-rag-review/). Run the text attachment with `python queue_probe.py.txt`; override its `REPO` constant when running elsewhere. The text extension keeps this standalone review attachment outside application lint and test discovery.

## GitHub synchronization

`origin/main` was freshly fetched at `db1eb8152513a2b9bab0de70aaeebc0323bea6ad`; the reviewed local HEAD is a descendant **141 commits ahead**. The unrelated `/root/code/rag-spark` checkout points to the same GitHub URL but contains an older implementation and was left untouched.

Preserve the reviewed code, existing local evaluation/doc/handoff work, and this report on `review/super-rag-2026-10-17`, with a draft pull request to `main`. `AGENTS.md` reserves merging/deployment for the coordinator; this publication does not merge or deploy the snapshot. Existing branch pointers and task status records are preserved.

Before publication, scanned all tracked and unignored files plus **1,076 incoming history blobs** for common OpenRouter/GitHub/AWS credentials and private-key markers. Incoming history had no matches; working-tree matches were existing synthetic test credentials/marker strings. No inspected file/blob exceeded 50 MB. This limited pattern scan is not a credential-audit guarantee.

## Handoff

TASK: Review Super RAG and update its GitHub repository.
CHANGE: Review report, verification logs and a queue reproduction; preserve and publish pre-existing project work.
WHY: Record independently reproduced delivery/race defects, current quality gates, and changes since the earlier review.
REUSED: Existing test suites, actual queue implementation, temporary SQLite databases; no new dependencies.
BUILT: A bounded concurrent-claim probe and review evidence only.
VERIFY: Commands and outcomes above.
RISKS: Findings 1–4 block acceptance; full backend execution and live ingestion/retrieval remain unverified. Coordinator merge required.
