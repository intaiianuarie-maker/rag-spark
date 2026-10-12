"""Pure workflow validator — no side effects, no execution.

Validates a ``WorkflowDefinition`` against a ``CapabilityManifest`` and returns
a ``ValidationResult``.  The validator is a pure function: it never touches
the filesystem, network, or any external state.
"""

from __future__ import annotations

from collections import Counter, deque

from .interfaces import (
    PORT_TYPE_ANY,
    CapabilityManifest,
    NodeType,
    ValidationError,
    ValidationResult,
    WorkflowDefinition,
)


def _types_compatible(source_type: str, target_type: str) -> bool:
    """Return True if a value of *source_type* can safely flow to *target_type*.

    Rules:
    - ``any`` is compatible with any type (source or target).
    - Identical type names are compatible.
    - All other combinations are incompatible (e.g. ``dict`` → ``number``).
    """
    if source_type == PORT_TYPE_ANY or target_type == PORT_TYPE_ANY:
        return True
    if source_type == target_type:
        return True
    return False


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
        """Validate *definition* against *manifest*. Returns all errors found."""
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

        ``any`` is compatible with all types.  Otherwise the source port
        type must equal the target port type.
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
                        f"'{edge.target_port}' ({tgt_type})"
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
