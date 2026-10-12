"""Langflow compatibility spike — W2-1: parser interface & strict security.

This slice implements the security boundary for the Langflow adapter:

* A restricted allowlist of standard declarative components (``_CATALOG``).
* Strict rejection of custom/untrusted Python code components with structured
  ``LangflowImportError`` rejections (fail-closed, no partial results).
* Defense-in-depth scan for executable-code markers in node templates.
* Resource guards (``MAX_NODES`` / ``MAX_EDGES``) against oversized flows.

Security invariants (fail-closed):

* Langflow's IDE can execute developer code and provides no tenant isolation
  (see https://docs.langflow.org/security).  This adapter therefore **never
  installs, imports, executes or loads Langflow** — it only translates plain
  JSON data structures into our own validated contracts.
* Only allowlisted declarative components are accepted.  Any other component —
  including custom Python code components, ``Code`` components, and components
  loaded from unvetted module paths — is **immediately rejected**.
* A defense-in-depth scan rejects any component whose template (or wrapper)
  carries executable-code markers, even when the component name itself is
  allowlisted.
* Size guards prevent resource-exhaustion imports before any graph work begins.

Version pin / audit note: the spike targets Langflow **1.x** export JSON
(``data.nodes`` / ``data.edges`` with ``data.sourceHandle`` catalog metadata and
``{source|handle|name}`` handle strings), recorded in ``LANGFOW_PIN``.
The exact handle/route encoding is version-specific; pin and audit the selected
Langflow version and transitive dependencies before production integration.

Nothing in this module executes, compiles or imports component code; the
resulting ``WorkflowDefinition`` must still pass ``WorkflowValidator`` and
``WorkflowCompiler`` before it can be dispatched by a runtime adapter.

Slice plan:
* W2-1 (this slice): parser interface + strict security rejection.
* W2-2: declarative node translation (TRIGGER, RETRIEVER, LLM, OUTPUT).
* W2-3: edge/condition routing mapping (CONDITION, TRANSFORM, AGENT) + export.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from ..interfaces import (
    PORT_TYPE_ANY,
    PORT_TYPE_BOOLEAN,
    PORT_TYPE_DICT,
    PORT_TYPE_LIST,
    PORT_TYPE_NUMBER,
    PORT_TYPE_STRING,
    NodeSpec,
    NodeType,
    PortSpec,
    WorkflowDefinition,
    WorkflowEdgeSpec,
    WorkflowRevision,
)

# ---------------------------------------------------------------------------
# Restricted component catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _IOPort:
    """Internal canonical port template (name, type, required)."""

    name: str
    type: str = PORT_TYPE_ANY
    required: bool = True


@dataclass(frozen=True)
class LangflowComponentSpec:
    """An allowlisted Langflow declarative component and its product mapping.

    ``config_fields`` maps a Langflow template field name to the key used in
    ``NodeSpec.config``; only these (JSON-scalar) fields become config, all
    other template metadata is ignored.  Connectable template fields must be
    declared as ports — the adapter rejects edges that reference anything else
    rather than silently guessing a mapping.
    """

    component: str
    node_type: NodeType
    display_name: str
    input_ports: tuple[_IOPort, ...] = ()
    output_ports: tuple[_IOPort, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    config_fields: Mapping[str, str] = dataclass_field(default_factory=dict)
    description: str = ""


#: Restricted allowlist of declarative components.  Extending this catalog
#: widens the attack surface and requires the same review as a new capability.
_CATALOG: tuple[LangflowComponentSpec, ...] = (
    # ---- TRIGGER -------------------------------------------------------- #
    LangflowComponentSpec(
        component="ChatInput",
        node_type=NodeType.TRIGGER,
        display_name="Chat Input",
        input_ports=(),
        output_ports=(_IOPort("message", PORT_TYPE_STRING),),
        description="Run start: initial message input.",
    ),
    LangflowComponentSpec(
        component="TextInput",
        node_type=NodeType.TRIGGER,
        display_name="Text Input",
        input_ports=(),
        output_ports=(_IOPort("message", PORT_TYPE_STRING),),
        description="Run start: initial text input.",
    ),
    # ---- RETRIEVER ------------------------------------------------------ #
    LangflowComponentSpec(
        component="VectorRetriever",
        node_type=NodeType.RETRIEVER,
        display_name="Vector Retriever",
        input_ports=(_IOPort("query", PORT_TYPE_STRING),),
        output_ports=(_IOPort("documents", PORT_TYPE_LIST),),
        required_capabilities=("retriever",),
        config_fields={"top_k": "top_k", "collection": "collection"},
        description="Restricted retrieval over the workspace vector store.",
    ),
    LangflowComponentSpec(
        component="VectorStoreRetriever",
        node_type=NodeType.RETRIEVER,
        display_name="Vector Store Retriever",
        input_ports=(_IOPort("query", PORT_TYPE_STRING),),
        output_ports=(_IOPort("documents", PORT_TYPE_LIST),),
        required_capabilities=("retriever",),
        config_fields={"top_k": "top_k", "collection": "collection"},
        description="Restricted retrieval over the workspace vector store.",
    ),
    LangflowComponentSpec(
        component="PGVectorRetriever",
        node_type=NodeType.RETRIEVER,
        display_name="PGVector Retriever",
        input_ports=(_IOPort("query", PORT_TYPE_STRING),),
        output_ports=(_IOPort("documents", PORT_TYPE_LIST),),
        required_capabilities=("retriever",),
        config_fields={"top_k": "top_k", "collection": "collection"},
        description="Restricted retrieval over the workspace vector store.",
    ),
    # ---- LLM ------------------------------------------------------------ #
    LangflowComponentSpec(
        component="ChatOpenAI",
        node_type=NodeType.LLM,
        display_name="Chat OpenAI",
        input_ports=(
            _IOPort("input_value", PORT_TYPE_STRING),
            _IOPort("system_message", PORT_TYPE_STRING, required=False),
        ),
        output_ports=(_IOPort("message", PORT_TYPE_STRING),),
        required_capabilities=("llm",),
        config_fields={
            "model_name": "model",
            "temperature": "temperature",
            "max_tokens": "max_tokens",
            "stream": "stream",
        },
        description="Verified-answer LLM step (no free model routing).",
    ),
    LangflowComponentSpec(
        component="OpenAIModel",
        node_type=NodeType.LLM,
        display_name="OpenAI Model",
        input_ports=(
            _IOPort("input_value", PORT_TYPE_STRING),
            _IOPort("system_message", PORT_TYPE_STRING, required=False),
        ),
        output_ports=(_IOPort("message", PORT_TYPE_STRING),),
        required_capabilities=("llm",),
        config_fields={
            "model_name": "model",
            "temperature": "temperature",
            "max_tokens": "max_tokens",
        },
        description="Verified-answer LLM step (no free model routing).",
    ),
    # ---- AGENT ---------------------------------------------------------- #
    LangflowComponentSpec(
        component="ToolCallingAgent",
        node_type=NodeType.AGENT,
        display_name="Tool-Calling Agent",
        input_ports=(
            _IOPort("input_value", PORT_TYPE_STRING),
            _IOPort("tools", PORT_TYPE_LIST, required=False),
        ),
        output_ports=(_IOPort("message", PORT_TYPE_STRING),),
        required_capabilities=("agent", "tool"),
        config_fields={
            "model_name": "model",
            "agent_instructions": "instructions",
            "temperature": "temperature",
            "max_tokens": "max_tokens",
        },
        description="Declarative tool-calling agent over approved tools.",
    ),
    # ---- TOOL ----------------------------------------------------------- #
    LangflowComponentSpec(
        component="Tool",
        node_type=NodeType.TOOL,
        display_name="Tool",
        input_ports=(_IOPort("input_value", PORT_TYPE_STRING),),
        output_ports=(_IOPort("output", PORT_TYPE_ANY),),
        required_capabilities=("tool",),
        config_fields={"tool_name": "tool", "tool_metadata": "tool_metadata"},
        description="Reference to an approved declarative tool by name.",
    ),
    # ---- CONDITION ------------------------------------------------------ #
    LangflowComponentSpec(
        component="ConditionalRouter",
        node_type=NodeType.CONDITION,
        display_name="Conditional Router",
        input_ports=(_IOPort("input_value", PORT_TYPE_ANY),),
        output_ports=(
            _IOPort("result", PORT_TYPE_BOOLEAN),
            _IOPort("message", PORT_TYPE_ANY),
        ),
        config_fields={"conditions": "routes"},
        description="Declarative route selection; selects a named route, "
        "never evaluates an expression.",
    ),
    # ---- TRANSFORM ------------------------------------------------------ #
    LangflowComponentSpec(
        component="Prompt",
        node_type=NodeType.TRANSFORM,
        display_name="Prompt",
        input_ports=(_IOPort("template", PORT_TYPE_STRING),),
        output_ports=(_IOPort("message", PORT_TYPE_STRING),),
        description="Bounded template interpolation (no eval/exec/imports).",
    ),
    LangflowComponentSpec(
        component="Template",
        node_type=NodeType.TRANSFORM,
        display_name="Template",
        input_ports=(_IOPort("template", PORT_TYPE_STRING),),
        output_ports=(_IOPort("message", PORT_TYPE_STRING),),
        description="Bounded template interpolation (no eval/exec/imports).",
    ),
    LangflowComponentSpec(
        component="Parser",
        node_type=NodeType.TRANSFORM,
        display_name="Parser",
        input_ports=(_IOPort("input_value", PORT_TYPE_ANY),),
        output_ports=(_IOPort("message", PORT_TYPE_STRING),),
        description="Bounded structured parse/clean step.",
    ),
    # ---- OUTPUT --------------------------------------------------------- #
    LangflowComponentSpec(
        component="ChatOutput",
        node_type=NodeType.OUTPUT,
        display_name="Chat Output",
        input_ports=(
            _IOPort("input_value", PORT_TYPE_ANY),
            _IOPort("message", PORT_TYPE_ANY, required=False),
        ),
        output_ports=(_IOPort("output", PORT_TYPE_ANY),),
        description="Verified answer sink.",
    ),
    LangflowComponentSpec(
        component="TextOutput",
        node_type=NodeType.OUTPUT,
        display_name="Text Output",
        input_ports=(
            _IOPort("input_value", PORT_TYPE_ANY),
            _IOPort("message", PORT_TYPE_ANY, required=False),
        ),
        output_ports=(_IOPort("output", PORT_TYPE_ANY),),
        description="Verified answer sink.",
    ),
)

#: Components whose *presence* means executable/custom Python code — rejected
#: by name even before the marker scan (Langflow's ``Code`` component and
#: custom/uploaded components).
_CODE_COMPONENT_NAMES = frozenset({
    "Code",
    "CustomComponent",
    "Component",
    "PythonFunction",
    "Function",
    "PythonREPL",
    "PythonExec",
    "Evaluate",
    "EvalExpression",
    "DynamicTool",
    "Import",
    "Shell",
    "Webhook",  # arbitrary HTTP is a separate approved capability (W6)
})

#: Field-name markers indicating executable/custom Python payloads.
#: Matched as substrings on normalized keys (lowercase, alphanumeric only).
_CODE_FIELD_MARKERS = frozenset({
    "code",
    "sourcecode",
    "pythoncode",
    "script",
    "exec",
    "eval",
    "evalstring",
    "lambda",
    "functionbody",
    "imports",
    "customcomponent",
    "class",
    "classname",
    "classpath",
    "module",
})

#: Maximum recursion depth for the security marker scan.
#: Prevents RecursionError on deeply nested or self-referential payloads.
MAX_DEPTH = 100

#: Maximum depth for template ``value``/``default`` chain extraction.
#: Real Langflow template chains are 1-3 deep; anything beyond this is
#: hostile and is rejected rather than followed.
MAX_TEMPLATE_DEPTH = 32


class _DepthExceeded:
    """Sentinel for template chains exceeding the depth cap or cyclic."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<template-depth-exceeded>"


_DEPTH_EXCEEDED = _DepthExceeded()

_LANGFLOW_TYPE_PREFIX = "langflow.io."

#: Canonical Langflow component used when exporting each product node type.
_CANONICAL_COMPONENT: dict[NodeType, str] = {
    NodeType.TRIGGER: "chatinput",
    NodeType.RETRIEVER: "vectorretriever",
    NodeType.LLM: "chatopenai",
    NodeType.AGENT: "toolcallingagent",
    NodeType.TOOL: "tool",
    NodeType.CONDITION: "conditionalrouter",
    NodeType.TRANSFORM: "prompt",
    NodeType.OUTPUT: "chatoutput",
}

#: Langflow template field types we can safely express (declarative only).
_LANGFLOW_FIELD_TYPES = {
    PORT_TYPE_STRING: "str",
    PORT_TYPE_BOOLEAN: "bool",
    PORT_TYPE_NUMBER: "float",
    PORT_TYPE_DICT: "dict",
    PORT_TYPE_LIST: "list",
    PORT_TYPE_ANY: "any",
}


class LangflowImportError(ValueError):
    """Raised when a flow cannot be safely translated to our contracts.

    The import is fail-closed: either a complete ``WorkflowDefinition`` is
    returned, or this error is raised with structured ``rejections``
    (``{code, message, node_id?, edge_id?}``) and no partial result.
    """

    def __init__(self, rejections: Sequence[Mapping[str, Any]]) -> None:
        self.rejections: list[dict[str, Any]] = [dict(r) for r in rejections]
        parts = [
            f"[{r.get('code', 'REJECTED')}] {r.get('message', '')}"
            f" (node={r.get('node_id', '-')}, edge={r.get('edge_id', '-')})"
            for r in self.rejections
        ]
        super().__init__(
            f"Langflow import rejected ({len(self.rejections)} finding(s)): "
            + "; ".join(parts)
        )


class LangflowAdapter:
    """Pure import/export translator for restricted Langflow flow definitions.

    Scope: W2-1 security boundary (parser interface + strict rejection),
    W2-2 declarative node translation (typed ``PortSpec``/``NodeSpec`` with
    allowlisted config), W2-3 edge/condition routing translation plus
    round-trip export.

    Usage::

        definition = LangflowAdapter.import_definition(
            flow_json, workflow_id="wf-1", workspace_id="ws-1",
        )
        revision = LangflowAdapter.import_revision(
            flow_json, workflow_id="wf-1", workspace_id="ws-1",
        )
        exported = LangflowAdapter.export_revision(revision)

    The adapter performs no I/O and never executes component code.  Imported
    definitions must still pass ``WorkflowValidator`` / ``WorkflowCompiler``.
    Config is populated only from explicitly allowlisted template fields
    (``LangflowComponentSpec.config_fields``); the security scan is never
    relied upon for config-mapping safety.
    """

    #: Spike version — recorded on every conversion for provenance.
    VERSION = "wf-langflow-adapter/1.0.0"
    #: Langflow export format this spike targets (pin and audit before
    #: production integration; see module docstring).
    LANGFOW_PIN = "langflow 1.x export JSON (data.nodes/data.edges)"
    #: Resource guards applied before any translation work.
    MAX_NODES = 200
    MAX_EDGES = 500
    MAX_FIELD_CHARS = 50_000

    # ------------------------------------------------------------------ #
    # Catalog
    # ------------------------------------------------------------------ #
    @staticmethod
    def catalog() -> dict[str, LangflowComponentSpec]:
        """Return the allowlisted components keyed by normalized name."""
        return {c.component.lower(): c for c in _CATALOG}

    @classmethod
    def is_component_supported(cls, component: str) -> bool:
        """Return True iff *component* is in the restricted allowlist."""
        return component.strip().lower() in cls.catalog()

    # ------------------------------------------------------------------ #
    # Import (W2-1: security validation + basic node resolution)
    # ------------------------------------------------------------------ #
    @classmethod
    def import_definition(
        cls,
        flow: Mapping[str, Any],
        *,
        workflow_id: str,
        workspace_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> WorkflowDefinition:
        """Translate a Langflow flow definition into a ``WorkflowDefinition``.

        W2-1: performs full security validation (allowlist + code scan +
        size guards) and returns a definition with basic node translation.
        Raises ``LangflowImportError`` (fail-closed) for any security issue.
        """
        rejections: list[dict[str, Any]] = []
        data = cls._flow_data(flow, rejections)
        if data is None:
            raise LangflowImportError(rejections)

        raw_nodes = data.get("nodes", [])
        raw_edges = data.get("edges", [])
        if not isinstance(raw_nodes, list) or not all(
            isinstance(n, Mapping) for n in raw_nodes
        ):
            rejections.append({
                "code": "BAD_FLOW_FORMAT",
                "message": "flow 'nodes' must be a list of node objects",
            })
            raise LangflowImportError(rejections)
        if not isinstance(raw_edges, list) or not all(
            isinstance(e, Mapping) for e in raw_edges
        ):
            rejections.append({
                "code": "BAD_FLOW_FORMAT",
                "message": "flow 'edges' must be a list of edge objects",
            })
            raise LangflowImportError(rejections)
        if len(raw_nodes) > cls.MAX_NODES:
            rejections.append({
                "code": "OVERSIZED_FLOW",
                "message": (
                    f"flow has {len(raw_nodes)} nodes, limit is {cls.MAX_NODES}"
                ),
            })
            raise LangflowImportError(rejections)
        if len(raw_edges) > cls.MAX_EDGES:
            rejections.append({
                "code": "OVERSIZED_FLOW",
                "message": (
                    f"flow has {len(raw_edges)} edges, limit is {cls.MAX_EDGES}"
                ),
            })
            raise LangflowImportError(rejections)
        if len(raw_nodes) == 0:
            rejections.append({
                "code": "EMPTY_FLOW",
                "message": "flow has no nodes",
            })
            raise LangflowImportError(rejections)

        # Security pass: resolve every component.  Any rejection aborts.
        resolved: dict[str, LangflowComponentSpec] = {}
        node_names: dict[str, str] = {}
        seen_node_ids: set[str] = set()
        for raw_node in raw_nodes:
            node_id = cls._node_id(raw_node)
            # F-3: duplicate node ID rejection
            if node_id is not None and node_id in seen_node_ids:
                rejections.append({
                    "code": "DUPLICATE_NODE_ID",
                    "message": f"node ID '{node_id}' appears more than once",
                    "node_id": node_id,
                })
                continue
            if node_id is not None:
                seen_node_ids.add(node_id)
            # F-5: enforce MAX_FIELD_CHARS on display name
            display_name = cls._node_display_name(raw_node)
            if len(display_name) > cls.MAX_FIELD_CHARS:
                rejections.append({
                    "code": "OVERSIZED_FIELD",
                    "message": (
                        f"node display name exceeds {cls.MAX_FIELD_CHARS} chars"
                    ),
                    "node_id": node_id,
                })
                continue
            # F-5: enforce MAX_FIELD_CHARS on template values
            rejections.extend(cls._check_template_sizes(raw_node, node_id))
            component, spec, node_rejections = cls._resolve_component(raw_node)
            rejections.extend(node_rejections)
            if spec is not None and component is not None and node_id is not None:
                resolved[node_id] = spec
                node_names[node_id] = display_name
            elif not node_rejections and (node_id is None or component is None):
                rejections.append({
                    "code": "BAD_FLOW_FORMAT",
                    "message": "node object is missing 'id' or component type",
                    "node_id": node_id,
                })
        if rejections:
            raise LangflowImportError(rejections)

        # F-2: extend marker scan to edges
        for raw_edge in raw_edges:
            edge_markers, edge_depth_exceeded = cls._find_code_markers(raw_edge)
            edge_id = raw_edge.get("id")
            if edge_depth_exceeded:
                rejections.append({
                    "code": "OVERSIZED_FLOW",
                    "message": (
                        "edge structure is deeper than the "
                        f"{MAX_DEPTH}-level scan limit; rejected (fail-closed)"
                    ),
                    "edge_id": edge_id if isinstance(edge_id, str) else None,
                })
            if edge_markers:
                rejections.append({
                    "code": "CUSTOM_CODE_COMPONENT",
                    "message": (
                        f"edge contains executable-code marker(s) "
                        f"{sorted(edge_markers)}; rejected"
                    ),
                    "edge_id": edge_id if isinstance(edge_id, str) else None,
                })
        if rejections:
            raise LangflowImportError(rejections)

        # W2-2: full node translation — typed ports + allowlisted config.
        nodes = [
            cls._build_node_spec(node_id, spec, node_names[node_id], raw_nodes)
            for node_id, spec in resolved.items()
        ]
        nodes_by_id = {n.id: n for n in nodes}

        # W2-3: edge translation (ports + condition routes).
        edges, edge_rejections = cls._build_edges(raw_edges, resolved, nodes_by_id)
        rejections.extend(edge_rejections)
        if rejections:
            raise LangflowImportError(rejections)

        capabilities = {
            cap
            for spec in resolved.values()
            for cap in spec.required_capabilities
        }
        # Flow-level name fallback is untrusted input: bounded by
        # MAX_FIELD_CHARS (reject, not truncate).  A caller-supplied
        # ``name=`` is trusted API input and needs no guard.
        flow_name = str(flow.get("name") or workflow_id)
        if name is None and len(flow_name) > cls.MAX_FIELD_CHARS:
            rejections.append({
                "code": "OVERSIZED_FIELD",
                "message": (
                    f"flow name exceeds {cls.MAX_FIELD_CHARS} chars"
                ),
            })
            raise LangflowImportError(rejections)
        return WorkflowDefinition(
            id=workflow_id,
            workspace_id=workspace_id,
            name=name or flow_name,
            description=description if description is not None else "",
            nodes=nodes,
            edges=edges,
            workspace_capabilities=capabilities,
        )

    @classmethod
    def import_revision(
        cls,
        flow: Mapping[str, Any],
        *,
        workflow_id: str,
        workspace_id: str,
        name: str | None = None,
        description: str | None = None,
        revision: int = 1,
    ) -> WorkflowRevision:
        """Import *flow* and publish it as an immutable ``WorkflowRevision``."""
        definition = cls.import_definition(
            flow,
            workflow_id=workflow_id,
            workspace_id=workspace_id,
            name=name,
            description=description,
        )
        return WorkflowRevision.create(definition, revision=revision)

    # ------------------------------------------------------------------ #
    # W2-3: Edge translation
    # ------------------------------------------------------------------ #
    @classmethod
    def _build_edges(
        cls,
        raw_edges: Sequence[Mapping[str, Any]],
        resolved: dict[str, LangflowComponentSpec],
        nodes_by_id: Mapping[str, NodeSpec],
    ) -> tuple[list[WorkflowEdgeSpec], list[dict[str, Any]]]:
        """Translate Langflow edges into typed ``WorkflowEdgeSpec`` objects.

        Handle names are resolved against the ports declared by the node
        catalog (case-insensitive); unresolvable handles are rejected rather
        than silently guessed.  CONDITION sources derive their route value
        from explicit edge metadata first, then the handle name.
        """
        rejections: list[dict[str, Any]] = []
        edges: list[WorkflowEdgeSpec] = []
        for index, raw_edge in enumerate(raw_edges):
            raw_id = raw_edge.get("id")
            edge_id = (
                raw_id
                if isinstance(raw_id, str) and raw_id
                else f"lf-edge-{index}"
            )
            edge_scope = {"edge_id": edge_id}

            source_id = cls._edge_ref(raw_edge, "source", "source_node_id")
            target_id = cls._edge_ref(raw_edge, "target", "target_node_id")
            if source_id is None or target_id is None:
                rejections.append({
                    **edge_scope,
                    "code": "BAD_FLOW_FORMAT",
                    "message": "edge is missing 'source' or 'target' node id",
                })
                continue

            source_spec = resolved.get(source_id)
            target_spec = resolved.get(target_id)
            if source_spec is None or target_spec is None:
                missing = source_id if source_spec is None else target_id
                rejections.append({
                    **edge_scope,
                    "node_id": missing,
                    "code": "BAD_FLOW_FORMAT",
                    "message": (
                        f"edge references unknown node '{missing}'; all edge "
                        "endpoints must be allowlisted components"
                    ),
                })
                continue

            source_node = nodes_by_id[source_id]
            target_node = nodes_by_id[target_id]
            source_handle = cls._handle_name(raw_edge, "source")
            target_handle = cls._handle_name(raw_edge, "target")

            source_port = cls._match_port(source_handle, source_node.output_ports)
            if source_port is None and source_spec.node_type is NodeType.CONDITION:
                # CONDITION route handles carry the route value ("true"/
                # "false"), not an output port name; data flows via the
                # passthrough port.
                source_port = next(
                    (p.name for p in source_node.output_ports
                     if p.name == "message"),
                    None,
                )
            if source_port is None:
                rejections.append({
                    **edge_scope,
                    "node_id": source_id,
                    "code": "UNRESOLVED_SOURCE_PORT",
                    "message": (
                        f"cannot determine the source output port of edge "
                        f"'{edge_id}'; declared outputs of '{source_id}': "
                        f"{[p.name for p in source_node.output_ports]}"
                    ),
                })
                continue

            target_port = cls._match_port(target_handle, target_node.input_ports)
            if target_port is None:
                rejections.append({
                    **edge_scope,
                    "node_id": target_id,
                    "code": "UNRESOLVED_TARGET_PORT",
                    "message": (
                        f"edge '{edge_id}' target handle '{target_handle}' "
                        f"does not map to a declared input port of "
                        f"'{target_id}': "
                        f"{[p.name for p in target_node.input_ports]}"
                    ),
                })
                continue

            condition_value: Any | None = None
            if source_spec.node_type is NodeType.CONDITION:
                condition_value = cls._condition_value(
                    raw_edge, source_handle, target_handle
                )
                if condition_value is None:
                    rejections.append({
                        **edge_scope,
                        "node_id": source_id,
                        "code": "CONDITION_EDGE_NO_VALUE",
                        "message": (
                            f"conditional edge '{edge_id}' has no derivable "
                            "route value"
                        ),
                    })
                    continue

            edges.append(WorkflowEdgeSpec(
                id=edge_id,
                source_node_id=source_id,
                source_port=source_port,
                target_node_id=target_id,
                target_port=target_port,
                condition_value=condition_value,
            ))
        return edges, rejections

    @staticmethod
    def _edge_ref(raw_edge: Mapping[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = raw_edge.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _handle_name(raw_edge: Mapping[str, Any], side: str) -> str | None:
        """Resolve the port name carried by a Langflow handle.

        Priority: ``data.{side}Handle.name`` (or ``selected_output``), then
        the ``{side}Handle`` string ``{node}|{port}|{name}`` form.
        """
        data = raw_edge.get("data")
        if isinstance(data, Mapping):
            handle = data.get(f"{side}Handle")
            if isinstance(handle, Mapping):
                name = handle.get("name")
                if isinstance(name, str) and name:
                    return name
                selected = handle.get("selected_output")
                if isinstance(selected, str) and selected:
                    return selected
        raw = raw_edge.get(f"{side}Handle")
        if isinstance(raw, str) and raw:
            parts = [part for part in raw.split("|") if part]
            if len(parts) >= 3:
                return parts[2]
            if len(parts) == 1:
                return parts[0]
        return None

    @staticmethod
    def _match_port(
        handle_name: str | None, ports: Sequence[PortSpec]
    ) -> str | None:
        """Resolve a Langflow handle name to a declared port name.

        Matching is case-insensitive (Langflow uses PascalCase handle names).
        When handle metadata is absent entirely and the node declares exactly
        one port of that direction, that port is used.  A present but
        unmatched handle name is an error (never silently remapped).
        """
        if handle_name:
            for port in ports:
                if port.name.lower() == handle_name.strip().lower():
                    return port.name
            return None
        if len(ports) == 1:
            return ports[0].name
        return None

    @staticmethod
    def _condition_value(
        raw_edge: Mapping[str, Any],
        source_handle: str | None,
        target_handle: str | None,
    ) -> Any | None:
        """Derive a declared route value for a CONDITION edge.

        Priority: explicit ``condition_value``/``condition``/``route`` on the
        edge, then the source handle name, then the target handle name.
        Boolean-ish strings ("true"/"false") are coerced to booleans.
        """
        raw: Any = None
        data = raw_edge.get("data")
        if isinstance(data, Mapping):
            for key in ("condition_value", "condition", "route"):
                if key in data and data[key] is not None:
                    raw = data[key]
                    break
        if raw is None:
            raw = source_handle or target_handle
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in {"true", "false"}:
                return lowered == "true"
            return raw
        return None

    # ------------------------------------------------------------------ #
    # W2-3: Export
    # ------------------------------------------------------------------ #
    @classmethod
    def export_definition(cls, definition: WorkflowDefinition) -> dict[str, Any]:
        """Export *definition* as a Langflow-1.x-shaped flow definition.

        The exported JSON is re-importable by :meth:`import_definition`
        (round-trip purity: importing the export reproduces the same
        ``WorkflowDefinition`` and content hash).
        """
        nodes_out: list[dict[str, Any]] = []
        for position, node in enumerate(definition.nodes):
            component = cls._component_for(node)
            nodes_out.append({
                "id": node.id,
                "type": component,
                "position": {"x": 250.0 * position, "y": 0.0},
                "data": {
                    "type": component,
                    "id": f"{component}-{position:04d}",
                    "node": {
                        "display_name": node.name or component,
                        "description": node.description,
                        "template": cls._template_for(node, component),
                    },
                },
            })
        nodes_by_id = {n.id: n for n in definition.nodes}
        edges_out = [
            cls._export_edge(edge, nodes_by_id)
            for edge in definition.edges
        ]
        return {
            "id": definition.id,
            "name": definition.name,
            "description": definition.description,
            "data": {"nodes": nodes_out, "edges": edges_out},
        }

    @classmethod
    def export_revision(cls, revision: WorkflowRevision) -> dict[str, Any]:
        """Export an immutable revision back to Langflow flow JSON."""
        exported = cls.export_definition(revision.spec)
        exported["data"]["revision"] = revision.revision
        exported["data"]["revision_hash"] = revision.content_hash
        return exported

    @staticmethod
    def _component_for(node: NodeSpec) -> str:
        """Return the canonical Langflow component name for *node*."""
        try:
            return _CANONICAL_COMPONENT[node.type]
        except KeyError as exc:
            raise ValueError(
                f"node type '{node.type}' has no restricted Langflow "
                "component mapping and cannot be exported"
            ) from exc

    @classmethod
    def _template_for(cls, node: NodeSpec, component: str) -> dict[str, Any]:
        """Build the Langflow template for an exported node."""
        template: dict[str, Any] = {}
        for port in node.output_ports:
            template[port.name] = {
                "type": "str",
                "name": port.name,
                "value": "",
                "show": False,
            }
        for port in node.input_ports:
            field_type = _LANGFLOW_FIELD_TYPES.get(port.type, "any")
            template[port.name] = {
                "type": field_type,
                "name": port.name,
                "required": port.required,
                "value": "" if port.default is None else port.default,
                "show": True,
            }
        spec = cls.catalog()[component]
        for langflow_field, config_key in spec.config_fields.items():
            if config_key in node.config:
                template[langflow_field] = {
                    "type": "str",
                    "name": langflow_field,
                    "value": node.config[config_key],
                    "show": False,
                }
        return template

    @staticmethod
    def _export_edge(
        edge: WorkflowEdgeSpec, nodes_by_id: Mapping[str, NodeSpec]
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "sourceHandle": {
                "type": "output",
                "id": edge.source_port,
                "name": edge.source_port,
                "output_types": ["Message"],
                "selected_output": edge.source_port,
            },
            "targetHandle": {
                "type": "input",
                "id": edge.target_port,
                "name": edge.target_port,
                "fieldType": "str",
                "input_types": ["Message"],
            },
        }
        if edge.condition_value is not None:
            data["condition_value"] = edge.condition_value
        return {
            "id": edge.id,
            "source": edge.source_node_id,
            "target": edge.target_node_id,
            "sourceHandle": (
                f"{edge.source_node_id}|{edge.source_port}|{edge.source_port}"
            ),
            "targetHandle": (
                f"{edge.target_node_id}|{edge.target_port}|{edge.target_port}"
            ),
            "data": data,
        }

    # ------------------------------------------------------------------ #
    # Security helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _flow_data(
        flow: Any,
        rejections: list[dict[str, Any]],
    ) -> Mapping[str, Any] | None:
        if not isinstance(flow, Mapping):
            rejections.append({
                "code": "BAD_FLOW_FORMAT",
                "message": "flow must be a JSON object",
            })
            return None
        data = flow.get("data", flow)
        if not isinstance(data, Mapping):
            rejections.append({
                "code": "BAD_FLOW_FORMAT",
                "message": "flow 'data' must be a JSON object",
            })
            return None
        for key in ("nodes", "edges"):
            if key not in data and key not in flow:
                rejections.append({
                    "code": "BAD_FLOW_FORMAT",
                    "message": f"flow is missing required '{key}'",
                })
                return None
        return data

    @staticmethod
    def _node_id(raw_node: Mapping[str, Any]) -> str | None:
        node_id = raw_node.get("id")
        return node_id if isinstance(node_id, str) and node_id else None

    @classmethod
    def _resolve_component(
        cls, raw_node: Mapping[str, Any]
    ) -> tuple[str | None, LangflowComponentSpec | None, list[dict[str, Any]]]:
        """Resolve (and security-check) the component of *raw_node*."""
        rejections: list[dict[str, Any]] = []
        node_id = raw_node.get("id") if isinstance(raw_node, Mapping) else None
        node_scope = {"node_id": node_id if isinstance(node_id, str) else None}

        data = raw_node.get("data")
        raw_type: Any = None
        if isinstance(data, Mapping):
            raw_type = data.get("type")
        if raw_type is None:
            raw_type = raw_node.get("type")
        if not isinstance(raw_type, str) or not raw_type.strip():
            return None, None, [{
                **node_scope,
                "code": "BAD_FLOW_FORMAT",
                "message": "node is missing its component type",
            }]

        normalized = cls._normalize_component(raw_type)
        if "." in raw_type and not raw_type.startswith(_LANGFLOW_TYPE_PREFIX):
            rejections.append({
                **node_scope,
                "code": "CUSTOM_CODE_COMPONENT",
                "message": (
                    f"component '{raw_type}' is loaded from an unvetted module "
                    f"path; only '{_LANGFLOW_TYPE_PREFIX}...' standard "
                    "components are accepted"
                ),
            })
        if normalized in _CODE_COMPONENT_NAMES:
            rejections.append({
                **node_scope,
                "code": "CUSTOM_CODE_COMPONENT",
                "message": (
                    f"component '{normalized}' carries executable Python code "
                    "and is rejected (no custom component execution)"
                ),
            })
        code_markers, depth_exceeded = cls._find_code_markers(raw_node)
        if depth_exceeded:
            rejections.append({
                **node_scope,
                "code": "OVERSIZED_FLOW",
                "message": (
                    f"component '{normalized}' has a structure deeper than "
                    f"the {MAX_DEPTH}-level scan limit; rejected (fail-closed)"
                ),
            })
        if code_markers:
            rejections.append({
                **node_scope,
                "code": "CUSTOM_CODE_COMPONENT",
                "message": (
                    f"component '{normalized}' contains executable-code "
                    f"marker(s) {sorted(code_markers)}; rejected"
                ),
            })
        if rejections:
            return normalized, None, rejections

        spec = cls.catalog().get(normalized.lower())
        if spec is None:
            rejections.append({
                **node_scope,
                "code": "UNSUPPORTED_COMPONENT",
                "message": (
                    f"component '{normalized}' is not in the restricted "
                    f"allowlist: {sorted(cls.catalog())}"
                ),
            })
            return normalized, None, rejections
        return normalized, spec, rejections

    @staticmethod
    def _normalize_component(raw_type: str) -> str:
        """Normalize a possibly module-qualified type to its short name."""
        return raw_type.strip().rsplit(".", 1)[-1]

    @classmethod
    def _find_code_markers(cls, value: Any) -> tuple[set[str], bool]:
        """Iteratively find executable-code markers in a node object.

        Uses BFS with a depth limit to prevent RecursionError on deeply
        nested or self-referential payloads.  Normalizes keys to lowercase
        alphanumeric and matches markers at key boundaries.

        Returns ``(found_markers, depth_exceeded)``.  ``depth_exceeded`` is
        True when the scan was truncated at ``MAX_DEPTH`` — the caller must
        reject such payloads (fail-closed), never accept partial coverage.
        """
        found: set[str] = set()
        depth_exceeded = False
        visited: set[int] = set()
        queue: deque[tuple[Any, int]] = deque([(value, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth > MAX_DEPTH:
                depth_exceeded = True
                continue
            if id(current) in visited:
                continue
            visited.add(id(current))
            if isinstance(current, Mapping):
                for key, sub in current.items():
                    if isinstance(key, str):
                        normalized = cls._normalize_key(key)
                        if normalized and cls._has_marker(normalized):
                            found.add(normalized)
                    # Check for {"type": "code", "value": "..."} pattern
                    if (
                        isinstance(sub, str)
                        and key == "type"
                        and sub == "code"
                    ):
                        found.add("code")
                    queue.append((sub, depth + 1))
            elif isinstance(current, (list, tuple)):
                for sub in current:
                    queue.append((sub, depth + 1))
        return found, depth_exceeded

    @staticmethod
    def _has_marker(normalized_key: str) -> bool:
        """Check if a normalized key matches any code marker.

        A key matches if it equals a marker, starts with a marker, or ends
        with a marker.  This catches bypass attempts like ``my_class`` or
        ``exec_string`` while avoiding false positives like ``description``
        (which contains ``script`` as a substring but doesn't start/end
        with it).
        """
        return any(
            normalized_key == marker
            or normalized_key.startswith(marker)
            or normalized_key.endswith(marker)
            for marker in _CODE_FIELD_MARKERS
        )

    @staticmethod
    def _normalize_key(key: str) -> str:
        """Normalize a key to lowercase alphanumeric for marker matching."""
        return "".join(c for c in key.lower() if c.isalnum())

    @staticmethod
    def _node_display_name(raw_node: Mapping[str, Any]) -> str:
        data = raw_node.get("data")
        if isinstance(data, Mapping):
            node_meta = data.get("node")
            if isinstance(node_meta, Mapping):
                display = node_meta.get("display_name")
                if isinstance(display, str) and display:
                    return display
        return ""

    @staticmethod
    def _template(raw_node: Mapping[str, Any]) -> dict[str, Any]:
        data = raw_node.get("data")
        if isinstance(data, Mapping):
            node_meta = data.get("node")
            if isinstance(node_meta, Mapping):
                template = node_meta.get("template")
                if isinstance(template, Mapping):
                    return dict(template)
        return {}

    @staticmethod
    def _template_value(entry: Any) -> Any:
        """Extract a JSON-safe template value (scalars/dicts/lists only).

        Follows ``value``/``default`` chains iteratively with an explicit
        stack, a depth cap (``MAX_TEMPLATE_DEPTH``) and cycle protection.
        Returns the ``_DEPTH_EXCEEDED`` sentinel when the chain is too deep
        or cyclic — the caller converts that into a structured rejection.
        """
        visited: set[int] = set()
        stack: list[tuple[Any, int]] = [(entry, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > MAX_TEMPLATE_DEPTH:
                return _DEPTH_EXCEEDED
            if id(current) in visited:
                return _DEPTH_EXCEEDED
            visited.add(id(current))
            if isinstance(current, Mapping):
                followed = False
                for key in ("value", "default"):
                    if key in current:
                        stack.append((current[key], depth + 1))
                        followed = True
                        break
                if not followed:
                    return None
            elif isinstance(current, (list, tuple)):
                return current
            elif isinstance(current, (str, bool, int, float)):
                return current
            else:
                return None
        return _DEPTH_EXCEEDED

    @classmethod
    def _build_node_spec(
        cls,
        node_id: str,
        spec: LangflowComponentSpec,
        display_name: str,
        raw_nodes: Sequence[Mapping[str, Any]],
    ) -> NodeSpec:
        """Translate one allowlisted node into a full ``NodeSpec``.

        Ports come from the component catalog (never from untrusted input).
        Config is populated **only** from the component's explicitly
        allowlisted template fields (advisory A-1: the security scan is
        never relied upon for config-mapping safety).  ``_template_value``
        is safe to call here: the security pass has already rejected any
        overly deep or cyclic chains via ``_check_template_sizes``.
        """
        raw_node = next((n for n in raw_nodes if n.get("id") == node_id), {})
        template = cls._template(raw_node)
        config: dict[str, Any] = {}
        for langflow_field, config_key in spec.config_fields.items():
            entry = template.get(langflow_field)
            value = cls._template_value(entry)
            if value is None or value is _DEPTH_EXCEEDED:
                continue
            config[config_key] = value
        return NodeSpec(
            id=node_id,
            type=spec.node_type,
            name=display_name or spec.display_name,
            config=config,
            input_ports=[
                PortSpec(
                    name=p.name,
                    type=p.type,
                    required=p.required,
                    description=spec.description,
                )
                for p in spec.input_ports
            ],
            output_ports=[
                PortSpec(name=p.name, type=p.type, description=spec.description)
                for p in spec.output_ports
            ],
            required_capabilities=list(spec.required_capabilities),
            description=spec.description,
        )

    @classmethod
    def _check_template_sizes(
        cls,
        raw_node: Mapping[str, Any],
        node_id: str | None,
    ) -> list[dict[str, Any]]:
        """Check all template scalar values against MAX_FIELD_CHARS.

        Returns a list of rejections for any field that exceeds the limit
        or has an excessively deep/cyclic ``value``/``default`` chain.
        """
        rejections: list[dict[str, Any]] = []
        template = cls._template(raw_node)
        for field_name, entry in template.items():
            value = cls._template_value(entry)
            if value is _DEPTH_EXCEEDED:
                rejections.append({
                    "code": "BAD_FLOW_FORMAT",
                    "message": (
                        f"template field '{field_name}' has an excessively "
                        "deep or cyclic value/default chain; rejected"
                    ),
                    "node_id": node_id,
                })
                continue
            if isinstance(value, str) and len(value) > cls.MAX_FIELD_CHARS:
                rejections.append({
                    "code": "OVERSIZED_FIELD",
                    "message": (
                        f"template field '{field_name}' exceeds "
                        f"{cls.MAX_FIELD_CHARS} chars"
                    ),
                    "node_id": node_id,
                })
        return rejections


__all__ = [
    "LangflowAdapter",
    "LangflowComponentSpec",
    "LangflowImportError",
]
