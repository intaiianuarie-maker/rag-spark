# Remediation plan: acceptance review findings

Status: plan only. No agents running, nothing committed.
Source: Opus medium acceptance review on a fresh `ko-accept` stack (SQuAD v2, HotpotQA, DocVQA, ChartQA, Romanian XQuAD, deepset prompt-injection samples).

## Approach

Split by file ownership so agents never edit the same file. Each ticket: failing test first, a "done when" line, and a failure-path test. Models follow the Artificial Analysis Pareto frontier: Sonnet high for implementation, Opus medium where the code decides trust or egress.

## Wave A: five agents in parallel

| Ticket | Bugs | Files | Model | Done when |
|---|---|---|---|---|
| A1 Fallback policy | B1, B15, B18 | `packages/models/adapters/openrouter_provider.py` (generate, stream) | Sonnet high | 400/401/403 and refusals never go to paid and are not retried. Only 429, 402, timeouts and 5xx escalate. `verifier_model` reports the model that actually served. Generation and ask payloads carry `provider.only` and `data_collection`. Mock-transport test per status code. |
| A2 Model-config egress | B7 | `apps/api/app/ask/router.py`, `packages/models/router_config.py`, model-configs store | Opus medium | `load_workspace_config` reads the DB row, not just the env var. `sensitive=true` forces the local-only route or a refusal, with no cloud call. Spy-transport test proves zero egress. |
| A3 Workflows (V4) | B2, B6, B11 | `packages/workflow_engine/production_executor.py`, `apps/api/app/workflows/router.py`, `credential_hygiene.py` | Sonnet high | OUTPUT fails the run when upstream `verified` is false. Router gets a real Stage 12 verifier and a Postgres chunk lookup. AGENT nodes run through the queued-agent runner (or are removed from the manifest if wiring proves unsafe). UUID `chunk_id` fields are exempt from the credential check; validate runs the same hygiene check as save. |
| A5 Answer quality | B8, B10, B13, B17 | `packages/answer/engine.py`, `semantic_verification.py`, Stage 12 verifier | Opus medium | A Stage 11 exception abstains instead of publishing. Exception text never reaches `answer.reason`. A new UNRESPONSIVE verdict makes a faithful but off-topic sentence abstain. A single parse fallback is retried once. |
| A6 Retrieval and readiness | B9, B12, B16 | `packages/retrieval/adapters/pg_fts.py`, ingest job status, `/ready` | Sonnet high | Lexical leg uses OR semantics for natural-language questions. Per-document graph status is exposed and the ask trace shows "graph pending". `/ready` stops counting a 404 as reachable. |

## Wave B: after A5

| Ticket | Bugs | Files | Model | Done when |
|---|---|---|---|---|
| A4 Agents and research (V3) | B3, B4, B5, B14 | `apps/api/app/agents/research.py`, `packages/agents/planner.py`, agent runtime, usage accounting | Sonnet high | Research persists in the agent store and is queued like `/runs/queued`, so it survives a restart. Agent and research final answers go through A5's Stage 11/12 path. Agent usage reports real completion tokens. Planner strips code fences. |

Waits for A5 because it reuses the verification path A5 changes.

## Wave C: proof

1. Run every touched test file, plus ruff, mypy and the vendor-import guard.
2. A Sonnet high agent replays the acceptance failures on a fresh `ko-accept`-style stack: 400/401/403 cases, sensitive-workspace egress, V4 OUTPUT and AGENT cases, research restart, SQuAD unanswerables.
3. A final Opus medium acceptance pass re-scores the feature table.

## Default decisions (change if you disagree)

- B13 abstains: a faithful but non-responsive answer is treated as unanswerable. This lowers the answered rate on SQuAD and Hotpot comparison questions but stops confident wrong-topic answers.
- V4 AGENT nodes get wired, not removed, because they are a plan feature.
- Research uses the existing sqlite agent store, no new table or migration.
- No commits until asked.

## Bug reference (from the acceptance review)

| ID | Severity | Summary |
|---|---|---|
| B1 | High | Any final free-model failure, including 400/401/403, escalates to paid (`openrouter_provider.py` ~568-569, stream ~772-773). Plan forbids paid fallback on 401/403/refusal. |
| B2 | High | V4 OUTPUT publishes upstream output even when the LLM node returned `verified=False`; no Stage 12 verifier configured in the router (`production_executor.py` ~115-117, router ~674). |
| B3 | High | V3 research runs inline, stored in a process dict, 404 after restart (`agents/research.py`). |
| B4 | Low | Agent usage reports `completion_tokens` 0. |
| B5 | High | Agents and research skip Stage 11/12; `sources=[]`. |
| B6 | Medium | V4 AGENT node validates and approves but fails at run: no production caller of `configure_agent_executor`. |
| B7 | High | Model-configs API (incl. `sensitive=true`) is ignored at ask time; ask reads only the `WORKSPACE_MODEL_CONFIG` env var. Egress leak. |
| B8 | Low | `LIFECYCLE_RECHECK_ERROR: {exc}` leaks exception text into `answer.reason` (`semantic_verification.py` ~584). |
| B9 | Medium | Document job is "ready" before graph extraction lands; asks right after ingest get 0 graph candidates. |
| B10 | Low-medium | A Stage 11 exception skips Stage 12 but the answer can still publish "verified" (`engine.py` ~635-670, ~951). |
| B11 | Medium | Any key ending `_id` is treated as a credential reference, so `citations[].chunk_id` fails save while validate says valid. |
| B12 | Medium | `plainto_tsquery` ANDs every term, so the lexical leg returns 0-1 candidates (`pg_fts.py` ~211-223). |
| B13 | Medium | Faithful but non-responsive sentences are published as verified (3/6 SQuAD unanswerables, Hotpot comparisons). |
| B14 | Low | Planner `json.loads` without stripping code fences (`planner.py` ~335). |
| B15 | Low | `verifier_model` reports the requested `:free` slug, not the serving model. |
| B16 | Low | `/ready` counts a 404 as reachable; rerank disclosure text is wrong when rerank is merely disabled. |
| B17 | Low | One PARSE_FALLBACK with no repair attempt. |
| B18 | Low | Provider allowlist (`provider.only`) and `data_collection` applied only to workflow/multimodal roles. |

## Risks and not covered

- A5 and A2 both touch the ask path (separate files); check the merged diff.
- Not covered by this plan: `GRAPH_MODE=rule`, web/git/local connector sync, deletion propagation via sync, workflow cancel and checkpoint resume, the repo worker Dockerfile on a network that blocks plain-HTTP apt, `docker-compose.dev.yml` run live, GitHub CI run. A final live pass can cover these.
- 17 unit failures and 3 errors under dead URLs need a live Postgres/Redis (notably `test_queue_semantics`); confirm in real CI.

## Cost

About six implementation agents, one live replay and one Opus pass.
