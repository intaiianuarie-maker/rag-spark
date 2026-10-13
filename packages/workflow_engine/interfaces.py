"""Workflow Engine domain interfaces (Phase 13 / Section 30).

Implements DAG-based workflow execution models: Node, Edge, Workflow, NodeRun,
WorkflowRun.  Also includes W0/W1 strict contracts: typed ports, versioned
revisions, capability manifest, validation results, compiled execution plans,
and the NodeExecutor Protocol.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NodeType(StrEnum):
    """Supported node types. Availability is gated by CapabilityManifest."""

    TRIGGER = "trigger"
    RETRIEVER = "retriever"
    LLM = "llm"
    AGENT = "agent"
    TOOL = "tool"
    CONDITION = "condition"
    TRANSFORM = "transform"
    HTTP = "http"
    OUTPUT = "output"


class NodeRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowRunStatus(StrEnum):
    """Terminal run states (T-V4-009: exactly one of the three).

    A run ends in exactly one of ``COMPLETED`` / ``FAILED`` / ``CANCELLED``:

    * ``COMPLETED`` — every node reached a genuine terminal state, no node
      failed, no declared join barrier was left incomplete, and every
      non-skipped node recorded its output.
    * ``FAILED`` — a node failed (raised, or returned an error payload) or a
      declared barrier could not be satisfied; the run names the node.
    * ``CANCELLED`` — cancellation was requested (or the task was
      cancelled) mid-run; in-flight nodes are settled and no node starts
      afterwards.  A cancelled run never becomes ``COMPLETED``.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowNode(BaseModel):
    """A single operational block in a workflow DAG (prototype model)."""

    id: str
    type: NodeType
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    """A directed dependency between two workflow nodes (prototype model)."""

    id: str
    source_node_id: str
    target_node_id: str
    condition_value: Any | None = None


class Workflow(BaseModel):
    """Definition of a runnable workflow DAG (prototype model)."""

    id: str
    workspace_id: str
    name: str
    description: str = ""
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# W0/W1: Strict contracts
# ---------------------------------------------------------------------------

PORT_TYPE_STRING = "string"
PORT_TYPE_DICT = "dict"
PORT_TYPE_LIST = "list"
PORT_TYPE_BOOLEAN = "boolean"
PORT_TYPE_NUMBER = "number"
PORT_TYPE_ANY = "any"
_SUPPORTED_PORT_TYPES = frozenset({
    PORT_TYPE_STRING,
    PORT_TYPE_DICT,
    PORT_TYPE_LIST,
    PORT_TYPE_BOOLEAN,
    PORT_TYPE_NUMBER,
    PORT_TYPE_ANY,
})

#: T-V4-007: maximum nesting depth accepted for a parameterized port type
#: (``list<list<…>>``).  The type value is an untrusted-length string, so the
#: recursive syntax check itself must be bounded.
_MAX_PORT_TYPE_DEPTH = 8


def _is_supported_port_type(value: str, depth: int = 0) -> bool:
    """Port-type syntax accepted by :class:`PortSpec` (T-V4-007).

    Grammar (stated, not emergent)::

        type      := base | "list<" type ">" , optional trailing "?"
        base      := string | number | boolean | dict | list | any

    * ``list<…>`` parameterizes the container with its element type,
      recursively — ``list<string>``, ``list<list<number>>`` — so validation
      can compare element types, not just the container.
    * a trailing ``?`` marks the port nullable (``string?``,
      ``list<string>?``); the meaning is enforced by the validator, the
      syntax is accepted here.
    * the bare bases (including ``list`` and ``any``) are unchanged.
    """
    if depth > _MAX_PORT_TYPE_DEPTH:
        return False
    if value.endswith("?"):
        value = value[:-1]
    if value in _SUPPORTED_PORT_TYPES:
        return True
    if value.startswith("list<") and value.endswith(">"):
        return _is_supported_port_type(value[5:-1], depth + 1)
    return False


class PortSpec(BaseModel):
    """A typed input or output port on a workflow node.

    Ports make data-flow contracts explicit.  An input port without a
    ``default`` value must be satisfied by an incoming edge or the initial
    workflow input, otherwise validation fails (required-ports check).

    T-V4-007: ``type`` also accepts parameterized and nullable forms —
    ``list<string>`` (element-typed container) and a trailing ``?``
    (``string?``, ``list<string>?``) for a port that accepts ``None``.
    See :func:`_is_supported_port_type`; the compatibility rules are stated
    as an explicit matrix in ``workflow_engine.validator``.
    """

    name: str
    type: str = PORT_TYPE_ANY
    required: bool = True
    default: Any = None
    description: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not _is_supported_port_type(self.type):
            raise ValueError(
                f"Unsupported port type '{self.type}'. "
                f"Allowed: bases {sorted(_SUPPORTED_PORT_TYPES)}, "
                f"recursive list<...> parameterization, and a trailing '?'"
            )


class JoinStrategy(StrEnum):
    """Declared join strategy for a node with multiple inbound edges (T-V4-008).

    The strategy is the node's explicit barrier contract — the compiler
    refuses to compile a join node that does not declare one:

    * ``all``   — wait for EVERY mapped inbound port; emit nothing until the
      barrier is complete.
    * ``any``   — wait for the FIRST inbound port to arrive; emit on partial
      input.
    * ``first`` — await only the first mapped port; later arrivals do not
      extend the barrier.
    """

    ALL = "all"
    ANY = "any"
    FIRST = "first"


class NodeSpec(BaseModel):
    """A workflow node with explicit port contracts and capability declarations.

    Unlike the prototype ``WorkflowNode``, this model requires the node to
    declare its input/output ports and the external capabilities it depends
    on.  The compiler and executor use these declarations to detect missing
    wiring and unavailable capabilities *before* any execution.

    T-V4-008: a node with two or more inbound edges is a join node and must
    declare ``join_strategy``; an undeclared join refuses to compile.
    """

    id: str
    type: NodeType
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    input_ports: list[PortSpec] = Field(default_factory=list)
    output_ports: list[PortSpec] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    description: str = ""
    #: Declared join strategy (``None`` = not declared; legal only for
    #: non-join nodes — single inbound edge or a trigger).
    join_strategy: JoinStrategy | None = None


class WorkflowEdgeSpec(BaseModel):
    """A typed edge routing a named output port to a named input port.

    Replaces the prototype ``WorkflowEdge`` which only carried node-level
    connectivity.  Port-level edges let the compiler verify that every
    required input is satisfied and that types are compatible.
    """

    id: str
    source_node_id: str
    source_port: str
    target_node_id: str
    target_port: str
    condition_value: Any | None = None


class WorkflowDefinition(BaseModel):
    """A workflow definition with port-level contracts and typed edges."""

    id: str
    workspace_id: str
    name: str
    description: str = ""
    nodes: list[NodeSpec] = Field(default_factory=list)
    edges: list[WorkflowEdgeSpec] = Field(default_factory=list)
    workspace_capabilities: set[str] = Field(default_factory=set)


class WorkflowRevision(BaseModel):
    """Immutable, versioned revision of a workflow definition.

    The ``content_hash`` is a SHA-256 digest of the canonical (sorted,
    JSON-serialised) spec.  Revisions are immutable: any change to the
    definition produces a new revision number and a new hash.  This is the
    unit the compiler operates on, ensuring a published revision never
    changes under users.

    The model is frozen (``frozen=True``) to prevent top-level field
    reassignment.  Post-construction mutations of nested spec fields are
    detected by hash verification in the compiler and executor.
    """

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    revision: int
    content_hash: str
    spec: WorkflowDefinition
    created_at: str = ""

    # ------------------------------------------------------------------ #
    # Hash construction / verification
    # ------------------------------------------------------------------ #
    @staticmethod
    def _canonical_payload(value: Any) -> Any:
        """Canonicalize a spec payload for hashing (T-V4-008).

        Dict keys are stringified and sorted, and set members are sorted by
        their canonical JSON.  Set iteration order depends on the process
        hash seed (``PYTHONHASHSEED``), so an unsorted set — e.g.
        ``workspace_capabilities`` — would make identical definition bytes
        hash differently across processes.  The dump is taken in python mode
        first so sets reach this normalizer as sets (not as seed-ordered
        lists).
        """
        if isinstance(value, dict):
            return {
                str(k): WorkflowRevision._canonical_payload(v)
                for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
            }
        if isinstance(value, (set, frozenset)):
            normalized = [WorkflowRevision._canonical_payload(v) for v in value]
            return sorted(
                normalized,
                key=lambda item: json.dumps(item, sort_keys=True, default=str),
            )
        if isinstance(value, (list, tuple)):
            return [WorkflowRevision._canonical_payload(v) for v in value]
        return value

    @staticmethod
    def _compute_content_hash(spec: WorkflowDefinition) -> str:
        """Compute the canonical SHA-256 of *spec* (sorted JSON).

        The canonicalization is process-independent (see
        :meth:`_canonical_payload`): the same definition bytes hash
        identically under any ``PYTHONHASHSEED``.
        """
        payload = WorkflowRevision._canonical_payload(spec.model_dump())
        canonical = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def create(
        cls,
        spec: WorkflowDefinition,
        *,
        revision: int = 1,
        created_at: str = "",
    ) -> WorkflowRevision:
        """Create a revision with a caller-computed content_hash.

        The hash is derived from the canonical JSON of *spec*, so identical
        specs always produce identical revisions.
        """
        h = cls._compute_content_hash(spec)
        return cls(
            workflow_id=spec.id,
            revision=revision,
            content_hash=h,
            spec=spec,
            created_at=created_at,
        )

    @model_validator(mode="after")
    def _verify_hash(self) -> WorkflowRevision:
        """Reject revisions whose content_hash does not match the spec."""
        computed = self._compute_content_hash(self.spec)
        if computed != self.content_hash:
            raise ValueError(
                f"content_hash mismatch: expected {computed}, "
                f"got {self.content_hash}"
            )
        return self


class CapabilityManifest(BaseModel):
    """Declares which node types and external capabilities are available.

    Nodes whose ``type`` is not in ``supported_node_types``, or whose
    ``required_capabilities`` are not in ``enabled_capabilities``, are
    rejected by the validator rather than silently downgrade-passthrough.
    """

    supported_node_types: set[NodeType] = Field(default_factory=set)
    enabled_capabilities: set[str] = Field(default_factory=set)


class ValidationError(BaseModel):
    """A single validation error with a stable machine-readable code."""

    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None


class ValidationResult(BaseModel):
    """Result of workflow validation. ``valid`` is True iff no errors."""

    valid: bool
    errors: list[ValidationError] = Field(default_factory=list)

    @property
    def error_summary(self) -> str:
        if self.valid:
            return "valid"
        return "; ".join(f"{e.code}: {e.message}" for e in self.errors)


class ExecutionEdgeInfo(BaseModel):
    """Describes one outgoing edge from a node in the execution plan.

    ``condition_value`` is set for edges originating from ``CONDITION`` nodes.
    The executor compares it against the source node's output on
    ``source_port`` to decide whether the edge is *active*.
    """

    target_node_id: str
    source_port: str
    target_port: str
    condition_value: Any | None = None


class ExecutionStep(BaseModel):
    """One step in a compiled execution plan.

    ``order`` gives the deterministic topological position.  For nodes with
    multiple inbound edges, ``join_mode`` describes how inputs are merged
    (``"all"`` = all predecessors must complete; ``"any"`` = any one
    suffices, used for condition-fan-out branches; ``"first"`` = only the
    first mapped port is awaited).

    T-V4-008: the join barrier is STATED on the step — ``join_waits_for``
    (which inbound ports the node waits for), ``join_emit_on_partial`` (what
    it emits when the barrier is incomplete) and ``join_timeout_s`` — so the
    executor never has to infer the barrier from ``join_mode``.
    """

    node_id: str
    order: int
    join_mode: str = "all"
    input_mapping: dict[str, list[str]] = Field(
        default_factory=dict,
        description="target_port -> list of 'source_node:source_port'",
    )
    outgoing_edges: list[ExecutionEdgeInfo] = Field(default_factory=list)
    #: Inbound ports this node waits for (sorted; strategy-dependent).
    join_waits_for: list[str] = Field(default_factory=list)
    #: What the node emits while the barrier is incomplete.
    join_emit_on_partial: str = "nothing"
    #: Barrier timeout in seconds (``None`` = the executor's run deadline).
    join_timeout_s: float | None = None


class ExecutionPlan(BaseModel):
    """Deterministic execution plan produced by the compiler.

    The plan is a pure data structure — no side effects, no execution.
    """

    revision_hash: str
    steps: list[ExecutionStep]
    trigger_node_id: str
    output_node_ids: list[str]

    def next_steps_after(self, completed_node_id: str) -> list[ExecutionStep]:
        """Return steps whose order is greater than the completed node's."""
        completed_orders = {
            s.order for s in self.steps if s.node_id == completed_node_id
        }
        if not completed_orders:
            return []
        max_order = max(completed_orders)
        ready: list[ExecutionStep] = []
        for step in self.steps:
            if step.order > max_order:
                ready.append(step)
        return ready

    def step_by_node_id(self, node_id: str) -> ExecutionStep | None:
        """Return the step for *node_id*, or ``None``."""
        return next((s for s in self.steps if s.node_id == node_id), None)
    def edge_to(
        self, target_node_id: str
    ) -> list[tuple[str, str, Any | None]]:
        """Return ``(source_node_id, source_port, condition_value)`` for each
        incoming edge of *target_node_id*."""
        result: list[tuple[str, str, Any | None]] = []
        for step in self.steps:
            for edge in step.outgoing_edges:
                if edge.target_node_id == target_node_id:
                    result.append(
                        (step.node_id, edge.source_port, edge.condition_value)
                    )
        return result


@runtime_checkable
class NodeExecutor(Protocol):
    """Execution-adapter Protocol — decouples node execution from
    validation/compilation.

    Concrete implementations may call real LLM/retriever/agent runtimes (W2+).
    W0/W1 uses ``FakeNodeExecutor`` for deterministic testing without
    installing or calling LangGraph/Langflow/real models.
    """

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any],
        workspace_id: str,
    ) -> dict[str, Any]:
        """Execute *node* and return a mapping of output_port_name -> value.

        If the node type or a required capability is not implemented, raise
        ``NotImplementedError`` (failure honesty — never mock success).
        """
        ...


class NodeRun(BaseModel):
    """Execution trace of an individual node in a workflow run.

    ``error`` carries the failure reason for FAILED nodes and the honest
    skip reason for SKIPPED ones (T-V4-009: a skip is disclosed, never
    silent).
    """

    node_id: str
    status: NodeRunStatus = NodeRunStatus.PENDING
    input_data: Any = None
    output_data: Any = None
    error: str | None = None
    latency_ms: float = 0.0


class WorkflowRun(BaseModel):
    """Execution instance of a workflow.

    T-V4-009: ``error`` / ``failed_node_id`` name the node that made the
    run terminal (failing node, incomplete-barrier node, or cancellation)
    so a non-COMPLETED run is never an unlabelled status.
    """

    id: str
    workspace_id: str
    workflow_id: str
    status: WorkflowRunStatus = WorkflowRunStatus.PENDING
    node_runs: dict[str, NodeRun] = Field(default_factory=dict)
    initial_input: dict[str, Any] = Field(default_factory=dict)
    final_output: Any = None
    created_at: str = ""
    finished_at: str | None = None
    #: Run-level failure/cancellation reason (``None`` on COMPLETED).
    error: str | None = None
    #: The node that determined the terminal status (``None`` on COMPLETED).
    failed_node_id: str | None = None
