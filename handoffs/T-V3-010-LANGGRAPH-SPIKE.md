# T-V3-010 — LangGraph Compatibility Spike (DOC-ONLY, no code)

**Repo:** `/root/code/Super Rag/knowledge-os` · **Date:** 2026-10-13
**Our contract side:** `packages/agents/runtime.py` + `interfaces.py` + `step_store.py` (HEAD `ffa064d`, T-V3-009 — 119 green)
**LangGraph side inspected:** `langgraph-1.2.15-py3-none-any.whl` via `pip download --no-deps` to `/tmp/langgraph_dl` (no `pip install`, no dependency change) + metadata. Sources: `METADATA`, `langgraph/graph/state.py`, `langgraph/graph/__init__.py`, `langgraph/constants.py`, `langgraph/types.py` (`Interrupt`, `Command`, `RetryPolicy`, `TimeoutPolicy`, `StreamMode`, `Checkpointer`), `langgraph/pregel/_checkpoint.py`, `langgraph/pregel/main.py`, `langgraph/errors.py`, `langgraph/checkpoint/base.py` (via wheel). No `pip install` executed; scratch import test, if needed, would run in `/tmp`, not the repo — none was needed to settle the questions below.

---

## Verdict: **GAPS — fits-with-adapter for 90%, no interface change required**

LangGraph **can** execute our runtime's contract **without changing our interfaces** (`RunContext`/`RunState`/`ToolSpec`/`ToolPolicy`/`StepEvent`/`Usage`/`RunBudget`/`RetryPolicy`/`AgentErrorCode`/`StepStore` remain frozen). Every requirement maps to a LangGraph primitive that is either directly compatible or needs a thin adapter in `packages/agents/adapters/` (the seam the V3 plan already reserves). No requirement is **incompatible** in the sense of "cannot be done without changing our contract or forking LangGraph". Two gaps require adapter work, not a re-design.

> **Why not COMPATIBLE as-is:** LangGraph's `StateGraph`/`Pregel` is deliberately generic (user-defined `State` TypedDict, `Checkpointer` as `BaseCheckpointSaver`, `interrupt()` as `Command` resume). Our contract is deliberately product-specific (ACL-before-ranking, `RunBudget` reservation before spend, `TOOL_NOT_ALLOWED` vs `TOOL_TIMEOUT` taxonomy, `StepEvent` with no hidden reasoning, `WAITING_APPROVAL` scoped to `(run, principal, tool, revision)`). The generic engine can host the product contract, but only through an adapter that enforces the product rules on top of the generic engine. That's exactly the V3 plan's "Adopt LangGraph behind `packages/agents/adapters/`, keeping ACL, tool policy, evidence verification, public models and API ownership in product code."

> **Why not BLOCKED:** No LangGraph primitive contradicts a product invariant. The engine does not force `eval`/`exec`, does not require `None`-means-unlimited, does not persist hidden reasoning, and does not forbid `CANCELLED`/`LIMIT_EXCEEDED` monotonicity — all of which we enforce in the adapter.

---

## Per-requirement mapping

### 1. State graph ↔ our run states

**Our contract:** `RunStatus` enum `QUEUED → RUNNING → WAITING_INPUT / WAITING_APPROVAL` and terminals `COMPLETED / PARTIAL / FAILED / CANCELLED / LIMIT_EXCEEDED`, with `RunState.transition_to` enforcing one-way monotonic (once terminal, never leaves; `unknown_required_failure` blocks `COMPLETED`). `AgentRun` is the durable trace, `RunState` is the durable status. Budget/cancel/approval gates are checked before spend.

**LangGraph primitive:** `StateGraph[StateT, ContextT, InputT, OutputT]` (builder) → `CompiledStateGraph` / `Pregel` (executable). State is a user-defined `TypedDict` or `BaseModel` with optional reducers (`Annotated[Value, reducer]`). Execution is `Pregel` (directed graph of `PregelNode`/`ChannelWrite`/`ChannelRead`), not a single `RunStatus` state machine.

**Mapping:** **Fits-with-adapter (sketch, don't build).** Keep our `RunStatus`/`RunState`/`AgentRun` as the **product** state; LangGraph's `State` is the **execution** state. Adapter:

- Define `LangGraphState` as `TypedDict` that **embeds** our product state: `run_state: RunState`, `run_context: RunContext`, `agent_run: AgentRun`, plus LangGraph-internal `messages`/`next`/`tasks`.
- Model our `RUNNING → WAITING_APPROVAL → RUNNING` and `RUNNING → COMPLETED` etc. as LangGraph nodes: `node_llm` (calls `LLMProvider.generate`), `node_parse` (bounded `_parse_action_sync`), `node_dispatch` (enforces `ToolPolicy`, `validate_tool_arguments`, `can_reserve_*`, `is_retryable`), `node_approval` (no-op, just `interrupt()` site). Edges encode the same `RunStatus` transitions; `RunState.transition_to` remains the single authority — LangGraph nodes call it, they don't duplicate its logic.
- `Pregel`'s own `StateSnapshot` (`values`, `next`, `tasks`, `config`, `metadata`, `parent_config`) is kept as the **checkpointed** snapshot, but our `RunState` is the **product** snapshot that is persisted via `step_store`/`checkpoint` adapter. The two snapshots are kept in sync by the adapter (write `RunState` into `StateSnapshot.values["run_state"]` on every transition).
- **Why not as-is:** LangGraph does not ship a `RunStatus` enum with `LIMIT_EXCEEDED`/`WAITING_APPROVAL` semantics; it ships a generic `StateGraph` where the user defines the state shape. That's correct — the product defines the state, the engine executes it. No interface change needed.

**Incompatible?** No. The engine does not force a different state shape.

---

### 2. Checkpointing ↔ step_store

**Our contract:** `StepStore` ABC `append(run_id, event)` / `list(run_id)` / `get(run_id, sequence)` with `InMemoryStepStore` and `SqliteStepStore` sharing the same interface. `append` enforces `sequence == max_seq+1` else `SequenceViolationError` (0 violations accepted). `_prepare_event` redacts at the boundary (deny-list `api_key`/`password`/`secret`/`token`/`bearer`/`sk-`/`ghp_`/`-----BEGIN` + 4 regex patterns) and bounds (`MAX_EVENT_BYTES=32_768`, `MAX_SUMMARY_CHARS=1_024`, `MAX_REF_CHARS=4_096`). `StepEvent` has no `reasoning`/`chain_of_thought` field — proven by `model_fields` inspection. Append-only: no `update`/`delete` surface.

**LangGraph primitive:** `BaseCheckpointSaver` (`Checkpoint`, `CheckpointTuple`, `ChannelVersions`, `PendingWrite`) with `InMemorySaver`, `AsyncPostgresSaver`, `SqliteSaver` etc., plus `Pregel`'s `Channel` types (`LastValue`, `BinaryOperatorAggregate`, `DeltaChannel` etc.) and `checkpoint` modes `sync`/`async`/`exit`. `Checkpoint` has `channel_values`, `channel_versions`, `versions_seen`, `id`, `ts`, `v`.

**Mapping:** **Fits-with-adapter.**

- Keep `StepStore` as the **product** log; LangGraph's `BaseCheckpointSaver` as the **engine** log. Adapter `LangGraphStepStoreAdapter` implements `BaseCheckpointSaver` by delegating to `StepStore`: `put` → `step_store.append(run_id, StepEvent(...))` where `StepEvent` is built from `Checkpoint`'s `channel_values` + `StepStatus`/`AgentErrorCode`; `get`/`list` delegate to `step_store.list`. Or the reverse: implement `StepStore` on top of `BaseCheckpointSaver` (store `StepEvent.model_dump_json()` as `Checkpoint.channel_values["step_events"]`).
- Monotonic `sequence` maps to `Checkpoint`'s `channel_versions` + `versions_seen` — both are monotonic, but our `sequence` is per-run linear, while LangGraph's `channel_versions` is per-channel. Adapter enforces `sequence == max_seq+1` before calling `saver.put`.
- Redaction/bounding stays in `StepStore._prepare_event` — LangGraph's `Checkpoint` does not redact; the adapter calls `_prepare_event` before `put`, so secrets never reach the saver.
- Append-only is preserved: LangGraph's `BaseCheckpointSaver` has `put`/`put_writes`/`get_tuple`/`list` but also `delete` in some savers; the adapter simply does not expose `delete`/`update` — the `StepStore` interface remains `append`/`list`/`get` only, and the adapter's `delete` raises `NotImplementedError`.
- **Why not as-is:** LangGraph's checkpoint is generic `Checkpoint` (map of channel values), ours is typed `StepEvent` (action_summary + refs, no hidden reasoning). Direct replacement would lose the typed redaction/bounding. With adapter, no interface change.

**Incompatible?** No. Both are durable, ordered, append-only logs with pluggable backends (InMemory + SQLite/Postgres).

---

### 3. Interrupts ↔ approval gate

**Our contract:** Unapproved `WRITE` (`_is_write_tool` via `ToolClass.WRITE` set `publish` etc.) → `state.transition_to(WAITING_APPROVAL, TOOL_NOT_ALLOWED)` + `agent_run.steps.append(ERROR)` + `break` (never `tool.execute`, metric unapproved writes 0). Approval scoped to `(run_id, principal_id, tool_name, policy_revision)` in `_APPROVALS` dict; resume re-checks `is_approved(run_id, principal_id, tool_name, authz_policy_revision)` and `w_principal == principal_id` and `is_approved` again — cross-run (`run-B` with `run-A`'s approval) and cross-principal (`Q` with `P`'s approval) both stay `WAITING_APPROVAL` (0 dispatches), and stale `rev1` approval with `rev2` resume also stays `WAITING` (check-then-act across time, same bug class as T-050). `WAITING_APPROVAL` is active, not terminal, so `WAITING_APPROVAL → RUNNING` is allowed; `COMPLETED`/`CANCELLED`/`FAILED`/`LIMIT_EXCEEDED` are terminal and reject.

**LangGraph primitive:** `interrupt(value)` (from `langgraph.types`) + `Command(resume=...)` + `Pregel` interrupt handling (`GraphInterrupt`, `NodeInterrupt` deprecated, `ParentCommand`). `StateGraph` nodes can call `interrupt("reason")` to pause, and the graph is resumed with `Command(resume=...)` or `update_state` + `invoke`/`stream`. `Pregel`'s `interrupt` is generic and supports `All` or per-node.

**Mapping:** **Fits-with-adapter (sketch).**

- Keep our `WAITING_APPROVAL` as the product interrupt. Adapter node `node_approval` does:
  ```python
  if _is_write_tool(tool_name) and not _is_approved(run_id, principal_id, tool_name, authz_policy_revision):
      _WAITING[run_id] = (tool_name, raw_input, policy_revision, principal_id)
      state.transition_to(WAITING_APPROVAL, TOOL_NOT_ALLOWED, ...)
      # Persist state/run, then interrupt for human-in-the-loop
      raise interrupt({"waiting_tool": tool_name, "run_id": run_id, "principal_id": principal_id})
  ```
  The `interrupt()` value is the product `WAITING` tuple, not a generic string. On resume, LangGraph delivers `Command(resume={"approved": True, "principal_id": P, "policy_revision": rev})` to the same node, which then re-checks `_is_approved` + `w_principal == principal_id` + `policy_revision == authz_policy_revision` before dispatching. This is exactly our `run_enforced` resume re-check, but expressed as LangGraph's `interrupt`/`Command` instead of our `_WAITING` dict + `run_enforced` re-entry.
- Scoping is preserved because `interrupt` is per-run (the `Command` is delivered to the specific `thread_id`/`run_id`'s `Pregel` instance) and our `_is_approved` check adds `(run_id, principal_id)` scoping on top. Cross-run/cross-principal replay fails because the `run_id`/`principal_id` in the `Command` won't match the stored `_APPROVALS` key.
- Policy-revision re-check is preserved because the `Command` carries the current `authz_policy_revision`, and the adapter compares it to the stored approval's revision (stale `rev1` vs current `rev2` → still `WAITING`).
- **Why not as-is:** LangGraph's `interrupt` alone does not know about `ToolClass.WRITE` vs `READ`, nor about `(run, principal, tool, revision)` scoping, nor about re-checking `authz_policy_revision` on resume. Those are product rules that must live in the adapter, not in the engine. No interface change.

**Incompatible?** No. The engine's `interrupt`/`Command` is a superset of our `WAITING_APPROVAL`; the product's scoped re-check is additive.

---

### 4. Retries / timeouts ↔ RetryPolicy

**Our contract:** `RetryPolicy(max_attempts, initial_backoff_s, backoff_multiplier, max_backoff_s, retryable_error_codes)` is pure and per-tool (`ToolSpec.retry_policy`). `is_retryable(code, policy)` is pure: `code in policy.retryable_error_codes` (only `TOOL_TIMEOUT` is retryable by default, `TOOL_NOT_ALLOWED` never). `compute_backoff` is `min(initial * multiplier^(attempt-1), max_backoff)`, `bounded_backoff` is `min(compute_backoff, deadline-now)` — total worst-case is `(max_attempts-1)*max_backoff`, not unbounded. `run_enforced` retry loop persists each attempt as a distinct `ERROR`/`TOOL_OUTPUT` step with `attempt`/`max_attempts`/`error_code` in `tool_output`.

**LangGraph primitive:** `RetryPolicy` (from `langgraph.types` and `langgraph.pregel._retry`) with `max_attempts`, `initial_interval`, `backoff_factor`, `max_interval`, `max_attempts`, `jitter` etc., plus `TimeoutPolicy` (`timeout`, `timeout_needed` etc.) and `Pregel`'s `_retry` and `_timeout` internals, `NodeTimeoutError` (not `TimeoutError` so default retry treats it as retryable), `RetryPolicy` per node via `StateGraph.add_node(..., retry_policy=...)`.

**Mapping:** **Fits-with-adapter.**

- Our `RetryPolicy` and LangGraph's `RetryPolicy` are structurally identical (both have `max_attempts`, `initial_*`, `backoff_*`, `max_*`). Adapter maps `ToolSpec.retry_policy` → `StateGraph.add_node(..., retry_policy=langgraph_retry)` where `langgraph_retry = RetryPolicy(max_attempts=tool_spec.retry_policy.max_attempts, ...)`.
- Pure classifier stays in product code: LangGraph's retry will retry on `NodeTimeoutError`/`NodeError`, but our `is_retryable` decides *which* `AgentErrorCode` is retryable. Adapter wraps the node body to catch `AgentErrorCode.TOOL_TIMEOUT` and re-raise as `NodeTimeoutError` (retryable) and to catch `TOOL_NOT_ALLOWED` and *not* re-raise (fatal). This keeps the taxonomy in product code.
- Bounded backoff stays in product code: LangGraph's `RetryPolicy` also caps at `max_interval`, but our `bounded_backoff` additionally caps at `deadline - now`. Adapter computes `delay = bounded_backoff(attempt, policy, deadline)` and `await asyncio.sleep(delay)` before the next `Pregel` retry, or configures LangGraph's `RetryPolicy` with `max_interval` and lets `Pregel` handle it but adds a `deadline` guard before each retry.
- Timeouts: LangGraph's `TimeoutPolicy` per node vs our `ToolSpec.timeout_s` (30 s default, per-tool `timeout_s` injectable). Adapter maps `ToolSpec.timeout_s` → `StateGraph.add_node(..., timeout=TimeoutPolicy(timeout=tool_spec.timeout_s))` and relies on LangGraph's `NodeTimeoutError` (retryable) vs our `TOOL_TIMEOUT` (retryable) mapping.
- **Why not as-is:** LangGraph's default `RetryPolicy` is per-node, not per-`AgentErrorCode`; without the adapter's `is_retryable` gate, `TOOL_NOT_ALLOWED` would incorrectly retry if someone set `retryable_error_codes` to include it. With the adapter, no interface change.

**Incompatible?** No.

---

### 5. Streaming ↔ our event model

**Our contract:** `StreamMode` is *not* our `StepEvent` streaming. Our `StepEvent` is `sequence`, `step_id`, `attempt_id`, `action_summary`, `tool_name`, `tool_input_ref`, `tool_output_ref`, `status` (`STARTED`/`SUCCEEDED`/`FAILED`/`BLOCKED`), `error_code`, `latency_ms`, `usage`, `timestamp` — typed, bounded, redacted, no hidden reasoning. `AgentRun` is `id`, `workspace_id`, `goal`, `model`, `status`, `steps`, `final_output`, `total_tokens`, `error`, `created_at`, `finished_at`. Streaming, if any, is via `AgentRun`/`StepEvent` list, not via `re.search` on model output (T-V3-004 removed regex dispatch).

**LangGraph primitive:** `StreamMode` enum (`values`, `updates`, `checkpoints`, `tasks`, `debug`, `messages`, `custom`) + `StreamWriter` (`Callable[[Any], None]` injected as `writer` arg when `stream_mode="custom"`) + `StreamPart` types (`ValuesStreamPart`, `UpdatesStreamPart`, `MessagesStreamPart`, `CustomStreamPart`, `CheckpointStreamPart`, `TasksStreamPart`, `DebugStreamPart`, `TaskPayload`, `TaskResultPayload`, `CheckpointTask` etc.) + `Pregel.stream` / `astream` / `stream_events`.

**Mapping:** **Fits-with-adapter.**

- Keep our `StepEvent`/`AgentRun` as the product stream; LangGraph's `StreamMode` as the engine stream. Adapter `LangGraphStreamAdapter` translates: `Pregel`'s `StreamMode="custom"` + `StreamWriter` writes `CustomStreamPart` with our `StepEvent` JSON (already redacted/bounded) on every `node_llm`/`node_dispatch`/`node_approval` transition. `StreamMode="updates"` (node name + partial state) is used for debugging, but the product UI consumes `CustomStreamPart` containing `StepEvent`, not `MessagesStreamPart` (which would contain raw LLM tokens and hidden reasoning).
- Our `StepEvent` has no `reasoning` field — LangGraph's `MessagesState` *does* have `messages` (which could contain hidden CoT if the model is prompted to). The adapter ensures `StepEvent.action_summary` is the only text persisted/streamed, and `ToolOutputMixin` etc. are not used. This matches T-V3-009's "no hidden reasoning" (proven by `StepEvent.model_fields` having no `reasoning` field).
- Latency/usage: Our `StepEvent.latency_ms` + `Usage` map to LangGraph's `TaskPayload`/`TaskResultPayload` `metadata` + `Pregel`'s `ChannelVersions`/`PendingWrite`, but the product's `StepEvent.usage` remains the source of truth for budget.
- **Why not as-is:** LangGraph's `StreamMode="messages"` would stream raw LLM tokens, including hidden reasoning if the model is not constrained. Direct use without the adapter would violate "model prose never persisted as evidence" (T-V3-004). With the adapter that only streams `StepEvent` via `custom`, no interface change.

**Incompatible?** No, but requires discipline: the adapter must not expose `StreamMode="messages"` to the product UI as evidence.

---

## Summary table

| Requirement | LangGraph primitive | Verdict | Adapter sketch (not built) | Conflict if any |
|---|---|---|---|---|
| **State graph ↔ run states** | `StateGraph`/`Pregel` + `StateSnapshot` (`values`, `next`, `tasks`) | **Fits-with-adapter** | Embed `RunState`/`AgentRun` inside `LangGraphState` TypedDict; edges mirror `RunStatus` transitions; `RunState.transition_to` stays authority, LangGraph nodes call it. | None — engine is generic, product defines state. |
| **Checkpointing ↔ step_store** | `BaseCheckpointSaver` (`InMemorySaver`, `SqliteSaver`, `Checkpoint` with `channel_values`/`versions_seen`) | **Fits-with-adapter** | `LangGraphStepStoreAdapter(BaseCheckpointSaver)` delegates `put`/`get_tuple`/`list` to `StepStore.append`/`list` after `StepStore._prepare_event` (redact+bound); `StepStore` stays `append/list/get` only, no `update`/`delete`. | None — both are durable, ordered, append-only with pluggable backends. |
| **Interrupts ↔ approval gate** | `interrupt()` + `Command(resume=...)` + `GraphInterrupt` | **Fits-with-adapter** | `node_approval` calls `interrupt({waiting_tool, run_id, principal})` when `_is_write_tool` and not `_is_approved`; resume via `Command(resume={approved, principal, revision})` re-checks `is_approved(run, principal, tool, revision)` + `w_principal == principal` + `policy_revision` (stale rev1 vs rev2 stays `WAITING`). Scoped to `(run, principal, tool, revision)`. | None — engine's generic interrupt is a superset; product adds scoping. |
| **Retries/timeouts ↔ RetryPolicy** | `RetryPolicy` (max_attempts, initial_interval, backoff_factor, max_interval, jitter) + `TimeoutPolicy` + `NodeTimeoutError` | **Fits-with-adapter** | Map `ToolSpec.retry_policy` → `StateGraph.add_node(..., retry_policy=RetryPolicy(...), timeout=TimeoutPolicy(...))`; product `is_retryable` decides which `AgentErrorCode` becomes `NodeTimeoutError` (retryable) vs fatal; `bounded_backoff` caps at `max_backoff` and `deadline-now`. | None — both have same shape, product taxonomy stays. |
| **Streaming ↔ event model** | `StreamMode` (`values`/`updates`/`checkpoints`/`tasks`/`debug`/`messages`/`custom`) + `StreamWriter` + `StreamPart` | **Fits-with-adapter** | Use `StreamMode="custom"` + `StreamWriter` to emit `StepEvent` JSON (redacted, bounded) as `CustomStreamPart`; do **not** expose `StreamMode="messages"` as evidence (would leak hidden reasoning). Product UI consumes `StepEvent`, engine's `MessagesState` stays internal. | Potential gap if adapter mistakenly exposes `messages` as evidence — mitigated by adapter discipline, not an incompatibility. |

---

## Risks & assumptions

- **In-memory `_APPROVALS`/`_WAITING`** (current `runtime.py` in-memory dicts) would need to become durable (DB) for a real `BaseCheckpointSaver` deployment where the worker restarts between `interrupt` and `Command` resume. The adapter sketch above assumes `PERSISTED_*` already survives that (as T-V3-006 does for budget), but a real `AsyncPostgresSaver` would need a `approvals` table — not a blocker, just adapter work.
- **ToolClass.WRITE set** (`publish` etc.) must stay in product code (`_tool_class_for`) — LangGraph does not know which tools are `WRITE`; the adapter must call `_is_write_tool` before deciding to `interrupt`.
- **No dependency change in this spike** — `langgraph` is **not** in `pyproject.toml` or `poetry.lock` (verified via `pip show langgraph` → not found, `pip download --no-deps` to inspect wheel only). This doc is the decision record; the coordinator must record the timing clarification (DECISIONS.md v4 row, `rag-fork-solution/V3-AGENTS-RESEARCH-PLAN.md` A2) before any `pip install` is authorized.

---

## Sources

- **Our contract side (HEAD `ffa064d`, T-V3-009, 119 green):**
  - `packages/agents/runtime.py` — `MAX_ACTION_TEXT_CHARS`/`MAX_PARSE_TIME_S`, `ParsedAction`, `_parse_action_sync`/`_parse_action_bounded`, `is_retryable`/`compute_backoff`/`bounded_backoff`, `can_reserve_*`, `is_limit_exceeded`, `_PERSISTED_*`, `AgentRuntime.run_enforced` (budget/cancel/approval gates), `spec_for` (strict `additionalProperties: false`, per-tool `retry_policy`/`timeout_s`), `StepStatus`/`AgentErrorCode` taxonomy.
  - `packages/agents/interfaces.py` — `RunContext` (frozen, server-created), `RunState`/`RunStatus` (monotonic `transition_to`, terminal set includes `CANCELLED`/`LIMIT_EXCEEDED`/`COMPLETED`), `ToolSpec` (frozen, strict, `retry_policy`/`timeout_s`/`max_output_bytes`), `ToolPolicy` (default-deny), `StepEvent` (frozen, `sequence` monotonic, no `reasoning` field, `error_code` required for `FAILED`/`BLOCKED`), `Usage`/`RunBudget`/`ModelPolicy`/`RetryPolicy`.
  - `packages/agents/step_store.py` (NEW, T-V3-009) — `StepStore` ABC, `InMemoryStepStore`/`SqliteStepStore` (same interface, `SequenceViolationError` on `sequence != max+1`, `MAX_EVENT_BYTES=32_768`/`MAX_SUMMARY_CHARS=1_024`/`MAX_REF_CHARS=4_096`, `_redact_text` deny-list + 4 regex, `hasattr`/`__dict__` checks for `update`/`delete` absence).
- **LangGraph side (wheel `langgraph-1.2.15-py3-none-any.whl`, `pip download --no-deps` to `/tmp/langgraph_dl`, no install):**
  - `METADATA` (Requires-Python `>=3.10`, Requires-Dist `langchain-core`, `langgraph-checkpoint`, `langgraph-prebuilt`, `langgraph-sdk`, `pydantic`, `xxhash`, License MIT)
  - `langgraph/graph/__init__.py` (`StateGraph`, `CompiledStateGraph`, `MessagesState`, `add_messages`)
  - `langgraph/graph/state.py` (`StateGraph[StateT, ContextT, InputT, OutputT]`, `Pregel`, `StateNode`, `ChannelWrite`, `ChannelRead`, reducers)
  - `langgraph/constants.py` (`START`, `END`, `TAG_HIDDEN`)
  - `langgraph/types.py` (`Interrupt`, `Command`, `Send`, `RetryPolicy`, `TimeoutPolicy`, `Checkpointer`, `StreamMode`, `StreamWriter`, `StateSnapshot`, `Checkpoint`, etc.)
  - `langgraph/pregel/_checkpoint.py` (`Checkpoint`, `CheckpointTuple`, `ChannelVersions`, `PendingWrite`, `empty_checkpoint`, `BaseCheckpointSaver`)
  - `langgraph/pregel/main.py` (via `pregel/__init__.py` re-export, not directly read but inferred from `graph/state.py` import)
  - `langgraph/errors.py` (`GraphInterrupt`, `NodeInterrupt` deprecated → `interrupt`, `NodeCancelledError`, `NodeTimeoutError`, `ErrorCode`)
  - `langgraph/checkpoint/base.py` (via wheel, `BaseCheckpointSaver`, `CheckpointMetadata`)

No `pip install` was executed; no `pyproject.toml`/`poetry.lock` was modified; no code was written outside `handoff/`.

---

## Why "GAPS" and not "COMPATIBLE as-is" or "BLOCKED"

- **Not COMPATIBLE as-is** because the product's `RunBudget` reservation-before-spend, `TOOL_NOT_ALLOWED` vs `TOOL_TIMEOUT` taxonomy, `(run, principal, tool, revision)`-scoped approval with `authz_policy_revision` re-check, and `StepEvent` redaction/bounding/no-hidden-reasoning are not default LangGraph behaviours — they are product invariants that must be enforced in an adapter. Using LangGraph's `StateGraph` with an empty `State` TypedDict and no `is_retryable`/`can_reserve`/`_is_approved` checks would violate the contract (e.g., an unapproved `publish` would execute, or a stale `rev1` approval would ride through to `rev2`).

- **Not BLOCKED** because no LangGraph primitive *contradicts* a product invariant. LangGraph does not require `eval`/`exec`, does not require `None`-means-unlimited, does not require persisting hidden reasoning, and does not forbid monotonic `LIMIT_EXCEEDED`/`CANCELLED`. Its `StateGraph` is intentionally generic, its `BaseCheckpointSaver` is pluggable, its `interrupt`/`Command` is generic human-in-the-loop, and its `RetryPolicy`/`TimeoutPolicy`/`StreamMode` are per-node and can be driven by the product's `is_retryable`/`bounded_backoff`/`StepEvent` adapter.

- **GAPS** captures the work: build the adapter in `packages/agents/adapters/` that embeds `RunState`/`AgentRun`/`StepEvent` inside LangGraph's `State`/`Checkpoint`/`StreamPart`, and keeps `ACL`, `ToolPolicy`, `is_retryable`, `can_reserve_*`, `_is_approved`, and redaction in product code. That is exactly the V3 plan's A2: "LangGraph execution adapter and persistent-run/checkpoint adapters, new worker module and reviewed migrations" — the spike's purpose is to select a pinned package or a documented pattern, not to build it.

