# Architecture Design Record: T-K07c Runtime State Seam

**Status**: Approved & Documented (Refactor-Scale Seam Design)  
**Ticket**: `T-K07c`  
**Supercedes / Realizes**: `T-K07b`  
**Author**: coordinator (`museR`)  
**Context**: Contabo Single-Node Production -> Scaled Distributed Cluster  

---

## 1. Context and Problem Statement

In the initial implementation of the V4 Workflow Execution Engine (`apps/api/app/workflows/store.py`), runtime execution state was maintained across multiple module-level globals:
- `_runs`: In-memory dictionary of active and finished workflow runs (`dict[tuple[str, str], WorkflowRun]`).
- `_approvals`: In-memory dictionary of human-in-the-loop approvals (`dict[tuple[str, str, int], WorkflowApproveResponse]`).
- `_executors`: In-memory registry of live `WorkflowExecutor` instances keyed by run ID (`dict[str, WorkflowExecutor]`).
- `_dispatch_tasks`: In-memory map of strong references to `asyncio.Task` instances executing runs (`dict[str, asyncio.Task[None]]`).

While SQLite persistence was partially hooked for drafts (`WORKFLOW_DRAFT_DB`) and runs (`WORKFLOW_RUNS_DB`), the live execution runtime state, cancellation handles, and task supervisors remained tightly coupled to in-process memory. This creates several architectural bottlenecks:
1. **Multi-Worker Process Isolation**: Running the API behind multiple Uvicorn workers or multi-replica containers causes executor registry lookup failures (e.g. canceling a run dispatched on worker A fails when requested on worker B with `409 no_live_executor`).
2. **Process Restarts & Crash Recovery**: If a worker process terminates mid-execution, orphaned runs cannot be cleanly reclaimed, resumed, or transitioned to terminal states by surviving workers.
3. **Testability & Determinism**: Unit and integration tests require resetting global state via delicate teardowns or monkey-patching.

---

## 2. Decision & Seam Design

We establish a formalized, injectable **Runtime State Seam** defined by the `WorkflowRuntimeStateStore` protocol in `packages/workflow_engine/runtime_state.py`.

```
               ┌─────────────────────────────────────┐
               │         FastAPI Workflow API        │
               │         (app/workflows/router.py)   │
               └──────────────────┬──────────────────┘
                                  │ Injectable Dependency
                                  ▼
               ┌─────────────────────────────────────┐
               │    WorkflowRuntimeStateStore        │
               │    (Runtime State Seam Protocol)    │
               └─────────┬───────────────────┬───────┘
                         │                   │
         ┌───────────────┴────────┐ ┌────────┴────────────────┐
         ▼                        ▼ ▼                         ▼
┌────────────────────────┐ ┌─────────────────────────────────────────┐
│ InMemoryRuntimeStore   │ │ PersistentDistributedRuntimeStore        │
│ (Unit Tests, Dev)      │ │ (Postgres / Redis + SQLite Checkpoints) │
└────────────────────────┘ └─────────────────────────────────────────┘
```

### 2.1 The `WorkflowRuntimeStateStore` Protocol

The runtime seam isolates three critical lifecycle domains:
1. **Run State & Metadata**: Monotonically advancing states (`PENDING` -> `RUNNING` -> terminal `COMPLETED`/`FAILED`/`CANCELLED`).
2. **Interactive Gates (Approvals)**: Human-in-the-loop checkpoints with durable resume payloads.
3. **Execution Handles & Task Control**: Injectable coordinator allowing distributed signaling for run cancellation and heartbeat lease renewals.

### 2.2 Core Interface Contract

```python
from typing import Protocol, runtime_checkable
from workflow_engine.interfaces import WorkflowRun, WorkflowRunStatus

@runtime_checkable
class WorkflowRuntimeStateStore(Protocol):
    """Injectable runtime state seam for workflow execution and supervision."""

    async def register_run(self, run: WorkflowRun) -> None:
        """Register a newly initialized workflow run."""
        ...

    async def get_run(self, workspace_id: str, run_id: str) -> WorkflowRun | None:
        """Lookup run state by workspace and run ID."""
        ...

    async def update_run_status(
        self,
        workspace_id: str,
        run_id: str,
        status: WorkflowRunStatus,
        error: str | None = None,
        final_output: Any = None,
    ) -> WorkflowRun:
        """Atomically transition run to a new lifecycle status."""
        ...

    async def register_approval(
        self,
        workspace_id: str,
        run_id: str,
        node_id: str,
        response: Any,
    ) -> None:
        """Record human approval outcome for a paused gate."""
        ...

    async def get_approval(
        self,
        workspace_id: str,
        run_id: str,
        node_id: str,
    ) -> Any | None:
        """Retrieve approval verdict for an interactive node."""
        ...

    async def request_cancellation(self, workspace_id: str, run_id: str) -> bool:
        """Signal cancellation across worker boundaries."""
        ...

    async def is_cancelled(self, run_id: str) -> bool:
        """Check whether cancellation was requested for run."""
        ...
```

---

## 3. Distributed Cluster & Multi-Worker Coordination

For scaled deployment across multiple nodes/workers:
1. **Cancellation Channel**: Rather than relying on in-process `_executors[run_id].cancel()`, worker processes listen on a lightweight pub/sub or polling channel (e.g. Postgres LISTEN/NOTIFY or Redis stream). When `request_cancellation` is invoked:
   - The status in the persistent store flips to `CANCEL_REQUESTED`.
   - The owning worker receives the cancellation notification and interrupts its active `asyncio.Task`.
   - The run executes boundary checkpoint save with status `CANCELLED`.
2. **Heartbeat & Lease Expiry**:
   - Each executing run maintains an active lease (default TTL 30s) renewed by the executor loop.
   - If a worker dies, the lease expires. A scavenger worker detects expired leases and either resumes execution from the latest `WorkflowCheckpoint` or marks the run `FAILED` with explicit crash disclosure.

---

## 4. Implementation Phasing & Backward Compatibility

- **Phase 1 (Completed in T-K07c)**:
  - Protocol definition in `packages/workflow_engine/runtime_state.py`.
  - Reference `InMemoryWorkflowRuntimeStore` implementation.
  - Contract test suite in `tests/unit/test_workflow_runtime_state_seam.py`.
- **Phase 2 (Future Maintenance Turn)**:
  - Wire `get_runtime_store()` into `apps/api/app/workflows/dependencies.py`.
  - Replace direct module global access in `store.py` with injected runtime store.
