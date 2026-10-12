# V4 W0/W1 Handoff — Strict Contracts, Validator, Compiler, Fake Runtime

**Agent**: v2 (`w4:p13`)
**Reviewer/sole merge authority**: coordinator `w4:p17`
**Status**: Implementation complete — awaiting review. Do not merge.
**Prepared**: 2026-10-09
**Scope**: V4 W0/W1 — versioned contracts, pure validator, deterministic compiler,
execution-adapter Protocol, fake-runtime tests. No LangGraph/LangFlow imports,
no real model/tool calls.

---

## 1. Changed files — exact

### Modified (additive only)
```
 packages/workflow_engine/__init__.py   |  41 +++++-
 packages/workflow_engine/interfaces.py | 255 ++++++++++++++++++++++++++++++++
 2 files changed, 290 insertions(+), 6 deletions(-)
```

### Created (new, untracked)
| File | Lines | Purpose |
|------|-------|---------|
| `packages/workflow_engine/validator.py` | 352 | Pure `WorkflowValidator` — 13 checks |
| `packages/workflow_engine/compiler.py` | 160 | Deterministic `WorkflowCompiler` |
| `packages/workflow_engine/executor.py` | 378 | `FakeNodeExecutor` + `WorkflowExecutor` |
| `tests/unit/test_workflow_validator.py` | 508 | 18 tests |
| `tests/unit/test_workflow_compiler.py` | 329 | 13 tests |
| `tests/unit/test_workflow_fake_runtime.py` | 479 | 14 tests |

### Untouched (preserved per constraints)
- `packages/workflow_engine/engine.py` — prototype **unchanged**
- `packages/workflow_engine/models/` — unchanged
- `tests/unit/test_workflows.py` — preserved (3 tests pass)
- `packages/agents/**`, `research/**`, `graph/**`, `answer/**`, `core/**`,
  retrieval/ingestion, app router, migrations, compose — **none edited**
---

## 2. Contracts added (interfaces.py)

### Port-level typed contracts
```python
class PortSpec(BaseModel):
    name: str
    type: str          # "string" | "dict" | "list" | "boolean" | "number" | "any"
    required: bool = True
    default: Any = None

class EdgePortRef(BaseModel):
    node_id: str
    port: str

class WorkflowEdgeSpec(BaseModel):
    id: str
    source: EdgePortRef
    target: EdgePortRef
    condition_value: Any | None = None

class NodeSpec(BaseModel):
    id: str
    type: NodeType
    config: dict[str, Any] = {}
    input_ports: list[PortSpec] = []
    output_ports: list[PortSpec] = []
    required_capabilities: list[str] = []  # e.g. ["llm"], ["retriever"]

class WorkflowDefinition(BaseModel):
    nodes: list[NodeSpec]
    edges: list[WorkflowEdgeSpec]
    workspace_capabilities: list[str] = []
```

### Immutable versioned revision
```python
class WorkflowRevision(BaseModel):
    workflow_id: str
    revision: int              # monotonically increasing
    content_hash: str           # SHA-256 of canonical JSON of spec
    spec: WorkflowDefinition    # immutable — do not mutate after publish
```
`content_hash` computed via `_content_hash()` from canonical JSON of
`spec.model_dump(mode="json", sort_keys=True)`. Same spec → same hash always.

### Capability manifest + Protocol
```python
class CapabilityManifest(BaseModel):
    supported_node_types: set[NodeType] = set()
    enabled_capabilities: set[str] = set()

@runtime_checkable
class NodeExecutor(Protocol):
    async def execute_node(
        self, node: NodeSpec, input_data: dict[str, Any], workspace_id: str
    ) -> dict[str, Any]: ...
```
Pure W0/W1 uses `FakeNodeExecutor` (no real calls). v3 will supply a
LangGraph-backed implementation in a coordinator-approved contract change (W3).

### Validation result with stable codes
```python
class ErrorCode(StrEnum):
    DUPLICATE_NODE_ID = "DUPLICATE_NODE_ID"
    DUPLICATE_EDGE_ID = "DUPLICATE_EDGE_ID"
    DANGLING_SOURCE = "DANGLING_SOURCE"
    DANGLING_TARGET = "DANGLING_TARGET"
    DANGLING_SOURCE_PORT = "DANGLING_SOURCE_PORT"
    DANGLING_TARGET_PORT = "DANGLING_TARGET_PORT"
    CYCLE_DETECTED = "CYCLE_DETECTED"
    UNSUPPORTED_NODE_TYPE = "UNSUPPORTED_NODE_TYPE"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    UNCONNECTED_REQUIRED_INPUT = "UNCONNECTED_REQUIRED_INPUT"
    NO_OUTPUT_NODE = "NO_OUTPUT_NODE"
    NO_TRIGGER = "NO_TRIGGER"
    ORPHANED_NODE = "ORPHANED_NODE"
    EMPTY_WORKFLOW = "EMPTY_WORKFLOW"
    CONDITION_EDGE_NO_VALUE = "CONDITION_EDGE_NO_VALUE"

class ValidationError(BaseModel):
    code: str
    message: str
    node_ids: list[str] = []
    edge_ids: list[str] = []

class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationError] = []
    @property
    def error_summary(self) -> str: ...
```

### Compiled execution plan
```python
class ExecutionEdgeInfo(BaseModel):
    target_node_id: str
    target_port: str
    condition_value: Any | None = None

class ExecutionStep(BaseModel):
    node_id: str
    node_type: NodeType
    join_mode: Literal["all", "any"] = "all"
    input_mapping: dict[str, str] = {}
    outgoing_edges: list[ExecutionEdgeInfo] = []

class ExecutionPlan(BaseModel):
    steps: list[ExecutionStep]
    trigger_node_id: str | None = None
    @property
    def step_by_node_id(self) -> dict[str, ExecutionStep]: ...
    def edge_to(self, node_id: str) -> list[str]: ...
    def next_steps_after(self, node_id: str) -> list[str]: ...
```

---

## 3. Validation rules (13 checks, pure & stateless)

| Check | Behavior |
|-------|----------|
| `EMPTY_WORKFLOW` | Rejects zero-node definitions |
| `DUPLICATE_NODE_ID` | No two nodes share an ID |
| `DUPLICATE_EDGE_ID` | No two edges share an ID |
| `DANGLING_SOURCE` | Edge source.node_id exists |
| `DANGLING_TARGET` | Edge target.node_id exists |
| `DANGLING_SOURCE_PORT` | Edge source.port declared on source node |
| `DANGLING_TARGET_PORT` | Edge target.port declared on target node |
| `CYCLE_DETECTED` | DFS back-edge detection (includes self-loops) |
| `UNSUPPORTED_NODE_TYPE` | Node type in CapabilityManifest |
| `UNSUPPORTED_CAPABILITY` | Required capabilities available |
| `UNCONNECTED_REQUIRED_INPUT` | Required input ports without edges/default (TRIGGER exempt) |
| `NO_OUTPUT_NODE` | At least one OUTPUT node |
| `NO_TRIGGER` | At least one TRIGGER node |
| `ORPHANED_NODE` | All nodes reachable via BFS from any trigger |
| `CONDITION_EDGE_NO_VALUE` | Edges from CONDITION nodes must have `condition_value` |

---

## 4. Compiler (deterministic, pure)

- **Algorithm**: Kahn's topological sort with sorted tie-breaking.
- **join_mode**: `"all"` for merge points; `"any"` downstream of CONDITION.
- **outgoing_edges**: populated from `WorkflowEdgeSpec` with `condition_value`.
- **Safety net**: re-validates against manifest before returning (permissive
  default if `manifest=None`).
- **Guarantee**: does not mutate input revision; performs no FS writes.

---

## 5. Executor (event-driven, uses NodeExecutor Protocol)

- `FakeNodeExecutor` implements `NodeExecutor` Protocol — verified by
  `test_fake_executor_implements_protocol`.
- **Trigger nodes**: receive `initial_input` as-is.
- **CONDITION nodes**: output `{"result": bool, "output": <passthrough input>}`
  — routing via `"result"`, data via `source_port` (`"output"`).
- **Conditional edges**: activated only when `result == condition_value`.
- **SKIPPED propagation**: inactive branch → SKIPPED; downstream all-join nodes
  also SKIPPED; other legal branches unaffected.
- **Failure honesty**: raises `NotImplementedError` for AGENT/TOOL/HTTP and
  unsupported capabilities — never mocks success.

---

## 6. Verification — commands + actual results

```bash
=== RUFF ===
All checks passed!

=== MYPY ===
Success: no issues found in 5 source files
(interfaces.py, validator.py, compiler.py, executor.py, __init__.py)

=== TESTS ===
48 passed in 0.45s
  test_workflow_validator.py      18 tests — all PASS
  test_workflow_compiler.py       13 tests — all PASS
  test_workflow_fake_runtime.py   14 tests — all PASS
  test_workflows.py (prototype)    3 tests — all PASS (preserved)
```

Exact command:
```bash
cd "/root/code/Super Rag/knowledge-os"
PYTHONPATH=packages /root/ko-venv/bin/python3 -m pytest tests/unit/test_workflow_validator.py tests/unit/test_workflow_compiler.py tests/unit/test_workflow_fake_runtime.py tests/unit/test_workflows.py -v --tb=short
```

---

## 7. Contract examples

### Minimal valid workflow (trigger → output)
```python
from workflow_engine import (WorkflowDefinition, NodeSpec, WorkflowEdgeSpec,
    EdgePortRef, NodeType, PortSpec)

definition = WorkflowDefinition(
    nodes=[
        NodeSpec(id="start", type=NodeType.TRIGGER,
                 input_ports=[PortSpec(name="input", type="dict", required=False)],
                 output_ports=[PortSpec(name="output", type="dict")]),
        NodeSpec(id="end", type=NodeType.OUTPUT,
                 input_ports=[PortSpec(name="input", type="dict")]),
    ],
    edges=[
        WorkflowEdgeSpec(id="e1",
            source=EdgePortRef(node_id="start", port="output"),
            target=EdgePortRef(node_id="end", port="input")),
    ],
)
```

### Conditional branch
```python
definition = WorkflowDefinition(
    nodes=[
        NodeSpec(id="ingress", type=NodeType.TRIGGER, ...),
        NodeSpec(id="checker", type=NodeType.CONDITION, ...),
        NodeSpec(id="legal", type=NodeType.OUTPUT, ...),
        NodeSpec(id="general", type=NodeType.OUTPUT, ...),
    ],
    edges=[
        WorkflowEdgeSpec(id="e1", source="ingress.output", target="checker.input"),
        WorkflowEdgeSpec(id="e2", source="checker.result", target="legal.input",
                         condition_value=True),
        WorkflowEdgeSpec(id="e3", source="checker.output", target="legal.input"),
        WorkflowEdgeSpec(id="e4", source="checker.result", target="general.input",
                         condition_value=False),
        WorkflowEdgeSpec(id="e5", source="checker.output", target="general.input"),
    ],
)
```
- `checker` emits `{"result": bool, "output": <input passthrough>}`
- `result==True` → `legal` runs, `general` SKIPPED
- `result==False` → `general` runs, `legal` SKIPPED
- Both branches receive `checker.output` via explicit edge (port `"input"`)

### Nested condition
```python
# checker_a → branch_a (TRANSFORM) → checker_b (CONDITION) → leaf_a / leaf_b
# If branch_a SKIPPED → checker_b SKIPPED → both leaves SKIPPED
```

---

## 8. Design decisions

1. **Routing vs data separation**: CONDITION nodes emit `"result"` (boolean,
   edge routing) and `"output"` (passthrough, data routing via `source_port`).
2. **Failure honesty**: FakeNodeExecutor raises `NotImplementedError` for
   AGENT/TOOL/HTTP and unsupported capabilities — no mock-success fallback.
3. **Deterministic compilation**: Kahn's algorithm with sorted tie-breaks.
   Same content hash → same `ExecutionPlan`.
4. **Pure functions**: Validator/Compiler do zero I/O, never mutate inputs.
   Executor writes only to in-memory run state.
5. **Capability gating**: Unsupported types/capabilities fail validation
   before execution. Nodes declare needs via `required_capabilities`.
6. **Protocol-based adapter**: `NodeExecutor` is `@runtime_checkable` Protocol.
   Fake implements structurally. v3 injects LangGraph-backed impl in reviewed
   contract change (W3) — no edits to v3 modules required.

---

## 9. Limitations

1. **No real runtime**: `FakeNodeExecutor` produces deterministic mock outputs.
   Real execution requires W3 (not started).
2. **No persistence**: In-memory runs only. Durable storage is W4.
3. **No ACL**: Cross-workspace denial, scope-widening checks are W4+.
4. **No scheduling**: Feature flags documented in plan but not wired (W6).
5. **No Postgres**: `WorkflowRevision` in-memory. Migrations are W4.
6. **G4/G5 graph gates**: Pending (V2 scope, not part of W0/W1).
7. **No Langflow export**: Spike deferred to W2 (coordinator approves deps).

---

## 10. Next suggested tasks (W2/W3 — coordinator approval required)

- **W2**: Langflow compatibility spike under `workflow_engine/adapters/**` —
  prove restricted round-trip. Must reject custom-code components.
- **W3**: Inject LangGraph-backed `NodeExecutor` (after accepted v3 contracts);
  real checkpoint/restart/cancel tests + verified artifact propagation.
- **W4**: Draft/revision persistence with reviewed Alembic migrations;
  authenticated CRUD/publish/run.

No shared-code dependency blockers at current scope.

---

## 11. Protected seams respected

- Did **not** modify `packages/agents/**`, `research/**`, `graph/**`,
  `answer/**`, `core/**`, retrieval/ingestion, app router, migrations,
  compose, or dependency manifests.
- Did **not** edit `tests/unit/test_workflows.py` (3 tests pass).
- Did **not** edit `packages/workflow_engine/engine.py` (prototype preserved).
- Did **not** switch branches, create worktrees, or merge.
- Did **not** import LangGraph/LangChain/LangFlow or any real model SDK.
- Proposed shared changes for W2+ are documented above; none required for
  W0/W1 acceptance.
