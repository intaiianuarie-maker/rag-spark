"""Pure workflow validator — no side effects, no execution.

Validates a ``WorkflowDefinition`` against a ``CapabilityManifest`` and returns
a ``ValidationResult``.  The validator is a pure function: it never touches
the filesystem, network, or any external state.

T-V4-006 adds the resource-bounds layer: graph node/edge counts, per-node
fan-out and total config size are capped so an oversized definition is
refused *before* the structural checks spend quadratic time on it.  The
bounds run as a pre-pass and short-circuit — the remaining checks are
O(n·m) in the hostile size, which is exactly what a fast refusal avoids.
"""

from __future__ import annotations

import json
from collections import Counter, deque
from collections.abc import Mapping
from typing import Any

from .interfaces import (
    CapabilityManifest,
    NodeType,
    ValidationError,
    ValidationResult,
    WorkflowDefinition,
)

# ---------------------------------------------------------------------------
# T-V4-006: resource bounds — every cap stated with its reason.
#
# A definition that passes these caps has a bounded validate/compile/hash
# cost: the structural checks below are at most O(n·m) with n ≤ 200 nodes
# and m ≤ 500 edges, and the revision hash serialises at most 1 MB of
# config.  The first two mirror the import-time caps in the Langflow adapter
# (``LangflowAdapter.MAX_NODES``/``MAX_EDGES``) so one policy governs both
# layers; the rest close the paths import-time caps cannot see (fan-out
# hubs and config totals across many nodes).
# ---------------------------------------------------------------------------

#: Node-count cap.  200 is already a very large authored workflow; the
#: structural checks (cycle DFS, per-edge node scans) are quadratic in it.
MAX_GRAPH_NODES = 200

#: Total edge-count cap, matching the adapter's import-time ``MAX_EDGES``.
#: 500 edges over ≤200 nodes is a dense DAG already.
MAX_GRAPH_EDGES = 500

#: Per-node fan-out (out-degree) cap.  Every catalog node declares at most
#: two output ports, and even a shared upstream node in a 200-node graph
#: fans out to at most a few dozen targets; 64 is generous for real DAGs
#: while refusing a 1:M hub whose adjacency work is linear in M every visit.
MAX_EDGES_PER_NODE = 64

#: Per-node config cap (bytes of canonical JSON).  Import caps string length
#: per field, not the sum; a node carrying many large-but-legal strings
#: would bloat the revision hash, which serialises the whole spec.
MAX_CONFIG_BYTES_PER_NODE = 32_768

#: Definition-wide config cap (bytes).  Layered above the per-node cap:
#: 200 nodes at the per-node cap would reach 6.4 MB, so the total keeps the
#: canonical JSON (recomputed on every hash verification) bounded.
MAX_CONFIG_BYTES_TOTAL = 1_048_576

#: Bound on echoed identifiers inside rejection messages, so an oversized
#: id cannot turn a rejection into an unbounded payload (B-slice practice).
_ECHO_CHARS = 64


def _echo(value: str) -> str:
    """Bounded echo of an untrusted identifier inside a rejection message."""
    return value if len(value) <= _ECHO_CHARS else value[:_ECHO_CHARS] + "…(truncated)"


def _config_bytes(container: Mapping[str, Any]) -> int:
    """Serialize *container* the way the revision-hash path does and size it.

    ``default=str`` keeps the measurement total (a value JSON cannot encode is
    rendered as its repr, mirroring how the hash path would carry it).  A
    circular reference — which the hash path cannot encode either — reports
    over the per-node cap so the node fails closed rather than measuring 0.
    """
    try:
        return len(json.dumps(container, sort_keys=True, default=str).encode("utf-8"))
    except ValueError:
        return MAX_CONFIG_BYTES_PER_NODE + 1


# ---------------------------------------------------------------------------
# T-V4-007: port type-compatibility — the matrix is STATED, not emergent
# ---------------------------------------------------------------------------

#: Base port types (mirrors ``interfaces._SUPPORTED_PORT_TYPES``).  A port
#: type is one of these, a recursive ``list<…>`` parameterization, and/or a
#: trailing ``?`` nullable marker.
_BASE_PORT_TYPES = frozenset({
    "string", "dict", "list", "boolean", "number", "any",
})

#: The nullable marker suffix (``string?`` — the port accepts ``None``).
_NULLABLE_SUFFIX = "?"

#: The compatibility matrix, stated explicitly: a ``(source, target)`` base
#: pair may be wired ONLY when it appears here.
#:
#: * identity pairs — a value flows only between ports of the same base type;
#: * ``any`` — the DECLARED wildcard.  ``any`` is in the matrix because the
#:   port that carries it explicitly opts out of type checking (both as a
#:   source and as a target).  It is not an emergent fallback: no other pair
#:   is inferred, and a pair absent from this matrix refuses.
_PORT_TYPE_MATRIX: frozenset[tuple[str, str]] = frozenset(
    {(base, base) for base in _BASE_PORT_TYPES}
    | {("any", base) for base in _BASE_PORT_TYPES}
    | {(base, "any") for base in _BASE_PORT_TYPES}
)

#: Container prefix/suffix for parameterized ports (``list<string>``).
_LIST_PREFIX = "list<"
_LIST_SUFFIX = ">"


def _split_nullable(port_type: str) -> tuple[str, bool]:
    """Split a trailing ``?`` off *port_type* → ``(base_type, nullable)``."""
    if port_type.endswith(_NULLABLE_SUFFIX):
        return port_type[: -len(_NULLABLE_SUFFIX)], True
    return port_type, False


def _list_element(port_type: str) -> str | None:
    """Element type of a ``list<…>`` port, or ``None`` for a bare ``list``.

    A bare ``list`` carries no element type — it is the legacy container
    form and behaves as ``list<any>`` (an explicitly untyped element, not a
    blanket bypass of element checking).
    """
    if port_type.startswith(_LIST_PREFIX) and port_type.endswith(_LIST_SUFFIX):
        return port_type[len(_LIST_PREFIX) : -len(_LIST_SUFFIX)]
    return None


def _types_compatible(source_type: str, target_type: str) -> bool:
    """Return True if a value of *source_type* can safely flow to *target_type*.

    The rules, in order (T-V4-007):

    1. **Matrix** — the base pair must be in :data:`_PORT_TYPE_MATRIX`
       (identity, or the declared ``any`` wildcard on either side).  A pair
       not in the matrix refuses — no silent coercion, no inference.
    2. **Nullability** — a nullable source (``string?``) may carry ``None``,
       so it may only feed a nullable target.  Nullability is per-port (the
       ``?`` on the port type), never a global escape hatch.
    3. **Generics** — ``list<A>`` into ``list<B>`` requires the ELEMENT types
       compatible under these same rules (recursively — same
       elements-not-container discipline as the B-R1-3 list fix).  A bare
       ``list`` is ``list<any>``.
    """
    source_base, source_nullable = _split_nullable(source_type)
    target_base, target_nullable = _split_nullable(target_type)

    # Rule 2: a possibly-None value never flows into a non-nullable port.
    if source_nullable and not target_nullable:
        return False

    # Rule 1: the CONTAINER pair must be in the stated matrix (a
    # parameterized list is still a ``list`` for the matrix).
    source_element = _list_element(source_base)
    target_element = _list_element(target_base)
    source_container = "list" if source_element is not None else source_base
    target_container = "list" if target_element is not None else target_base
    if (source_container, target_container) not in _PORT_TYPE_MATRIX:
        return False

    # Rule 3: compare element types when both sides are parameterized lists.
    if source_element is not None and target_element is not None:
        return _types_compatible(source_element, target_element)

    # A parameterized list into a bare list (or vice versa) is the
    # ``any``-element case already settled by the matrix.
    return True


def _mismatch_reason(source_type: str, target_type: str) -> str:
    """Bounded reason class for a refused pair (for the rejection message)."""
    source_base, source_nullable = _split_nullable(source_type)
    target_base, target_nullable = _split_nullable(target_type)
    if source_nullable and not target_nullable:
        return "nullable source into non-nullable target"
    source_element = _list_element(source_base)
    target_element = _list_element(target_base)
    source_container = "list" if source_element is not None else source_base
    target_container = "list" if target_element is not None else target_base
    if (
        source_element is not None
        and target_element is not None
        and (source_container, target_container) in _PORT_TYPE_MATRIX
    ):
        return f"list element types differ ({source_element} vs {target_element})"
    return "type pair not in the compatibility matrix"


class WorkflowValidator:
    """Stateless validator for workflow definitions.

    Usage::

        manifest = CapabilityManifest(
            supported_node_types={NodeType.TRIGGER, NodeType.LLM,
                                  NodeType.OUTPUT, NodeType.CONDITION},
            enabled_capabilities={"llm"},
        )
        result = WorkflowValidator.validate(definition, manifest)
        if not result.valid:
            raise ValueError(result.error_summary)
    """

    @staticmethod
    def validate(
        definition: WorkflowDefinition,
        manifest: CapabilityManifest,
        ) -> ValidationResult:
        """Validate *definition* against *manifest*. Returns all errors found.

        T-V4-006: the resource bounds run first and short-circuit.  Past a
        cap the structural checks below are quadratic in the hostile size
        (cycle DFS, per-edge node scans, per-node config serialisation), so
        refusing immediately is what keeps an oversized definition from
        hanging the caller; undersized definitions behave exactly as before.
        """
        resource_errors = WorkflowValidator._check_resource_bounds(definition)
        if resource_errors:
            return ValidationResult(valid=False, errors=resource_errors)

        errors: list[ValidationError] = []

        errors.extend(WorkflowValidator._check_duplicate_node_ids(definition))
        errors.extend(WorkflowValidator._check_duplicate_edge_ids(definition))
        errors.extend(WorkflowValidator._check_duplicate_port_names(definition))
        errors.extend(WorkflowValidator._check_dangling_edges(definition))
        errors.extend(WorkflowValidator._check_cycles(definition))
        errors.extend(WorkflowValidator._check_capabilities(definition, manifest))
        errors.extend(WorkflowValidator._check_port_connectivity(definition))
        errors.extend(WorkflowValidator._check_type_compatibility(definition))
        errors.extend(WorkflowValidator._check_output_nodes(definition))
        errors.extend(WorkflowValidator._check_connectivity(definition))
        errors.extend(WorkflowValidator._check_conditional_edges(definition))

        return ValidationResult(
            valid=(len(errors) == 0),
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # T-V4-006: resource bounds (pre-pass — see ``validate``)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_resource_bounds(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Refuse oversized graphs fast: node/edge counts, fan-out, config size.

        Every check is O(n + m) in the definition itself and never grows the
        input: counts are ``len()``, fan-out is one Counter pass over the
        edges, and config size is measured with a single serialisation per
        node (the same serialisation the revision hash performs).
        """
        errors: list[ValidationError] = []
        node_count = len(definition.nodes)
        edge_count = len(definition.edges)

        if node_count > MAX_GRAPH_NODES:
            errors.append(ValidationError(
                code="TOO_MANY_NODES",
                message=(
                    f"workflow has {node_count} nodes; the limit is "
                    f"{MAX_GRAPH_NODES}"
                ),
            ))
        if edge_count > MAX_GRAPH_EDGES:
            errors.append(ValidationError(
                code="TOO_MANY_EDGES",
                message=(
                    f"workflow has {edge_count} edges; the limit is "
                    f"{MAX_GRAPH_EDGES}"
                ),
            ))

        if edge_count:
            out_degree: Counter[str] = Counter()
            for edge in definition.edges:
                out_degree[edge.source_node_id] += 1
            for source_id in sorted(out_degree):
                degree = out_degree[source_id]
                if degree > MAX_EDGES_PER_NODE:
                    errors.append(ValidationError(
                        code="NODE_FAN_OUT_TOO_HIGH",
                        message=(
                            f"node '{_echo(source_id)}' has {degree} outgoing "
                            f"edges; the limit is {MAX_EDGES_PER_NODE}"
                        ),
                        node_id=source_id,
                    ))

        total_config = 0
        for node in definition.nodes:
            size = _config_bytes(node.config)
            total_config += size
            if size > MAX_CONFIG_BYTES_PER_NODE:
                errors.append(ValidationError(
                    code="NODE_CONFIG_TOO_LARGE",
                    message=(
                        f"node '{_echo(node.id)}' carries {size} bytes of "
                        f"config; the limit is {MAX_CONFIG_BYTES_PER_NODE}"
                    ),
                    node_id=node.id,
                ))
        if total_config > MAX_CONFIG_BYTES_TOTAL:
            errors.append(ValidationError(
                code="GRAPH_CONFIG_TOO_LARGE",
                message=(
                    f"workflow carries {total_config} bytes of config in "
                    f"total; the limit is {MAX_CONFIG_BYTES_TOTAL}"
                ),
            ))

        return errors


    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_duplicate_node_ids(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Reject duplicate node IDs."""
        ids = [n.id for n in definition.nodes]
        counts = Counter(ids)
        errors: list[ValidationError] = []
        for node_id, count in counts.items():
            if count > 1:
                errors.append(ValidationError(
                    code="DUPLICATE_NODE_ID",
                    message=f"Node ID '{node_id}' appears {count} times",
                    node_id=node_id,
                ))
        return errors

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_duplicate_edge_ids(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Reject duplicate edge IDs."""
        ids = [e.id for e in definition.edges]
        counts = Counter(ids)
        errors: list[ValidationError] = []
        for edge_id, count in counts.items():
            if count > 1:
                errors.append(ValidationError(
                    code="DUPLICATE_EDGE_ID",
                    message=f"Edge ID '{edge_id}' appears {count} times",
                    edge_id=edge_id,
                ))
        return errors

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_dangling_edges(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Reject edges that reference non-existent nodes or ports."""
        node_ids = {n.id for n in definition.nodes}
        node_output_ports: dict[str, set[str]] = {
            n.id: {p.name for p in n.output_ports} for n in definition.nodes
        }
        node_input_ports: dict[str, set[str]] = {
            n.id: {p.name for p in n.input_ports} for n in definition.nodes
        }
        errors: list[ValidationError] = []

        for edge in definition.edges:
            if edge.source_node_id not in node_ids:
                errors.append(ValidationError(
                    code="DANGLING_SOURCE",
                    message=(
                        f"Edge '{edge.id}' references unknown source node "
                        f"'{edge.source_node_id}'"
                    ),
                    edge_id=edge.id,
                ))
                continue
            if edge.target_node_id not in node_ids:
                errors.append(ValidationError(
                    code="DANGLING_TARGET",
                    message=(
                        f"Edge '{edge.id}' references unknown target node "
                        f"'{edge.target_node_id}'"
                    ),
                    edge_id=edge.id,
                ))
                continue
            # Check port existence
            if edge.source_port and edge.source_port not in node_output_ports[edge.source_node_id]:
                errors.append(ValidationError(
                    code="DANGLING_SOURCE_PORT",
                    message=(
                        f"Edge '{edge.id}' source port '{edge.source_port}' "
                        f"not declared on node '{edge.source_node_id}'"
                    ),
                    edge_id=edge.id,
                ))
            if edge.target_port and edge.target_port not in node_input_ports[edge.target_node_id]:
                errors.append(ValidationError(
                    code="DANGLING_TARGET_PORT",
                    message=(
                        f"Edge '{edge.id}' target port '{edge.target_port}' "
                        f"not declared on node '{edge.target_node_id}'"
                    ),
                    edge_id=edge.id,
                ))
        return errors

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_cycles(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Reject cyclic graphs via DFS back-edge detection."""
        adj: dict[str, list[str]] = {}
        for node in definition.nodes:
            adj[node.id] = []
        for edge in definition.edges:
            adj.setdefault(edge.source_node_id, []).append(edge.target_node_id)

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {n: WHITE for n in adj}
        errors: list[ValidationError] = []

        def _dfs(node_id: str, path: list[str]) -> None:
            color[node_id] = GRAY
            path.append(node_id)
            for neighbor in adj.get(node_id, []):
                if neighbor not in color:
                    continue  # dangling edge — reported elsewhere
                if color[neighbor] == GRAY:
                    cycle = [*path[path.index(neighbor):], neighbor]
                    errors.append(ValidationError(
                        code="CYCLE_DETECTED",
                        message=f"Cycle detected: {' -> '.join(cycle)}",
                        node_id=neighbor,
                    ))
                elif color[neighbor] == WHITE:
                    _dfs(neighbor, path)
            path.pop()
            color[node_id] = BLACK

        for nid in list(adj.keys()):
            if color[nid] == WHITE:
                _dfs(nid, [])

        return errors

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_capabilities(
        definition: WorkflowDefinition,
        manifest: CapabilityManifest,
    ) -> list[ValidationError]:
        """Reject nodes whose type or required capabilities are unavailable."""
        errors: list[ValidationError] = []
        available_caps = manifest.enabled_capabilities

        for node in definition.nodes:
            if node.type not in manifest.supported_node_types:
                errors.append(ValidationError(
                    code="UNSUPPORTED_NODE_TYPE",
                    message=(
                        f"Node '{node.id}' uses unsupported type "
                        f"'{node.type}'. Supported: {sorted(manifest.supported_node_types)}"
                    ),
                    node_id=node.id,
                ))
            for cap in node.required_capabilities:
                if cap not in available_caps:
                    errors.append(ValidationError(
                        code="UNSUPPORTED_CAPABILITY",
                        message=(
                            f"Node '{node.id}' requires capability '{cap}' "
                            f"which is not in the manifest"
                        ),
                        node_id=node.id,
                    ))
        return errors

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_port_connectivity(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Ensure every required input port (without default) is connected.

        TRIGGER nodes are exempt -- they receive ``initial_input`` at run time.
        """
        errors: list[ValidationError] = []
        edge_targets: dict[str, set[str]] = {
            n.id: set() for n in definition.nodes
        }
        for edge in definition.edges:
            if edge.target_node_id in edge_targets:
                edge_targets[edge.target_node_id].add(edge.target_port)

        for node in definition.nodes:
            if node.type == NodeType.TRIGGER:
                continue  # trigger receives initial_input
            for port in node.input_ports:
                if port.required and port.default is None:
                    if port.name not in edge_targets.get(node.id, set()):
                        errors.append(ValidationError(
                            code="UNCONNECTED_REQUIRED_INPUT",
                            message=(
                                f"Required input port '{port.name}' on node "
                                f"'{node.id}' (type '{node.type}') is not "
                                f"connected by any edge"
                            ),
                            node_id=node.id,
                        ))
        return errors

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_duplicate_port_names(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Reject duplicate or empty port names within a node.

        Duplicate port names make the type contract ambiguous: the compiler
        and executor cannot determine which port a value belongs to.
        """
        errors: list[ValidationError] = []
        for node in definition.nodes:
            all_ports = list(node.input_ports) + list(node.output_ports)
            # Check for empty names.
            for port in all_ports:
                if not port.name:
                    errors.append(ValidationError(
                        code="EMPTY_PORT_NAME",
                        message=f"Node '{node.id}' has a port with an empty name",
                        node_id=node.id,
                    ))
            # Check for duplicates (non-empty only — empties reported above).
            names = [p.name for p in all_ports if p.name]
            counts = Counter(names)
            for name, count in counts.items():
                if count > 1:
                    errors.append(ValidationError(
                        code="DUPLICATE_PORT_NAME",
                        message=f"Duplicate port name '{name}' on node '{node.id}'",
                        node_id=node.id,
                    ))
        return errors

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_type_compatibility(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Reject edges whose source/target port types are incompatible.

        The compatibility rules are the stated matrix + nullability +
        element-type recursion in ``_types_compatible`` (T-V4-007).  A
        refusal names the edge, both ports and both types, plus the reason
        class (off-matrix pair, nullable source into non-nullable target, or
        mismatched list element types) so the operator sees WHY.
        """
        errors: list[ValidationError] = []

        # Index port types by (node_id, port_name).
        port_types: dict[tuple[str, str], str] = {}
        for node in definition.nodes:
            for p in node.output_ports:
                port_types[(node.id, p.name)] = p.type
            for p in node.input_ports:
                port_types[(node.id, p.name)] = p.type

        for edge in definition.edges:
            src_type = port_types.get((edge.source_node_id, edge.source_port))
            tgt_type = port_types.get((edge.target_node_id, edge.target_port))
            if src_type is None or tgt_type is None:
                continue  # dangling — reported by _check_dangling_edges
            if not _types_compatible(src_type, tgt_type):
                errors.append(ValidationError(
                    code="TYPE_MISMATCH",
                    message=(
                        f"Edge '{edge.id}': source port '{edge.source_port}' "
                        f"({src_type}) is incompatible with target port "
                        f"'{edge.target_port}' ({tgt_type}) — "
                        f"{_mismatch_reason(src_type, tgt_type)}"
                    ),
                                        edge_id=edge.id,
                ))
        return errors

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_output_nodes(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Ensure at least one OUTPUT node exists."""
        errors: list[ValidationError] = []
        output_nodes = [
            n for n in definition.nodes if n.type == NodeType.OUTPUT
        ]
        if not output_nodes:
            errors.append(ValidationError(
                code="NO_OUTPUT_NODE",
                message="Workflow must contain at least one OUTPUT node",
            ))
        return errors

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_connectivity(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Ensure every non-trigger node is reachable from a trigger node.

        Unreachable nodes indicate orphaned branches that would never execute.
        """
        errors: list[ValidationError] = []
        if not definition.nodes:
            errors.append(ValidationError(
                code="EMPTY_WORKFLOW",
                message="Workflow has no nodes",
            ))
            return errors

        node_ids = {n.id for n in definition.nodes}
        trigger_ids = {
            n.id for n in definition.nodes if n.type == NodeType.TRIGGER
        }
        if not trigger_ids:
            errors.append(ValidationError(
                code="NO_TRIGGER",
                message="Workflow must contain at least one TRIGGER node",
            ))
            return errors

        # BFS from all triggers.
        adj: dict[str, list[str]] = {n.id: [] for n in definition.nodes}
        for edge in definition.edges:
            if edge.source_node_id in node_ids and edge.target_node_id in node_ids:
                adj[edge.source_node_id].append(edge.target_node_id)

        reachable: set[str] = set()
        queue: deque[str] = deque(sorted(trigger_ids))
        while queue:
            curr = queue.popleft()
            if curr in reachable:
                continue
            reachable.add(curr)
            for neighbor in sorted(adj.get(curr, [])):
                if neighbor not in reachable:
                    queue.append(neighbor)

        unreachable = node_ids - reachable
        for nid in sorted(unreachable):
            errors.append(ValidationError(
                code="ORPHANED_NODE",
                message=f"Node '{nid}' is not reachable from any trigger node",
                node_id=nid,
            ))
        return errors

    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_conditional_edges(
        definition: WorkflowDefinition,
    ) -> list[ValidationError]:
        """Ensure conditional edges from CONDITION nodes have condition_value."""
        errors: list[ValidationError] = []
        for edge in definition.edges:
            source_node = next(
                (n for n in definition.nodes if n.id == edge.source_node_id),
                None,
            )
            if source_node is None:
                continue  # dangling — reported elsewhere
            if source_node.type == NodeType.CONDITION and edge.condition_value is None:
                errors.append(ValidationError(
                    code="CONDITION_EDGE_NO_VALUE",
                    message=(
                        f"Conditional edge '{edge.id}' from CONDITION node "
                        f"'{source_node.id}' has no condition_value"
                    ),
                    edge_id=edge.id,
                ))
        return errors
