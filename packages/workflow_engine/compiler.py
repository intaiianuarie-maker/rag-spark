"""Deterministic workflow compiler.

Compiles a validated ``WorkflowRevision`` into an ``ExecutionPlan``.
The compiler is pure: it produces a data structure describing execution
order and data-flow wiring.  It performs no I/O, no execution, and no
side effects.
"""

from __future__ import annotations

from collections import deque

from .interfaces import (
    CapabilityManifest,
    ExecutionEdgeInfo,
    ExecutionPlan,
    ExecutionStep,
    NodeType,
    WorkflowEdgeSpec,
    WorkflowRevision,
)
from .validator import WorkflowValidator


class WorkflowCompiler:
    """Compiles a ``WorkflowRevision`` into a deterministic ``ExecutionPlan``.

    The compiler assumes the revision has already passed validation (it
    re-validates against the manifest as a safety net, but does not mutate
    or execute anything).
    """

    @staticmethod
    def compile(
        revision: WorkflowRevision,
        *,
        manifest: CapabilityManifest | None = None,
        allow_unvalidated: bool = False,
    ) -> ExecutionPlan:
        """Compile a revision into an ``ExecutionPlan``.

        Parameters
        ----------
        revision:
            A ``WorkflowRevision`` with a valid, validated ``WorkflowDefinition``.
        manifest:
            Capability manifest to re-validate against.  If ``None`` and
            ``allow_unvalidated`` is ``False``, a permissive manifest is used
            (all node types supported, no capability requirements).
        allow_unvalidated:
            If ``True``, skip re-validation.  Use only in tests that have
            already validated.
        """
        # Verify revision integrity: recompute hash and reject mismatches.
        # This detects post-construction mutations to the spec (nested or
        # top-level) that would invalidate the plan identity.
        computed_hash = WorkflowRevision._compute_content_hash(revision.spec)
        if computed_hash != revision.content_hash:
            raise ValueError(
                f"Revision content_hash mismatch: spec has been modified "
                f"since publishing (expected {computed_hash}, "
                f"got {revision.content_hash})"
            )

        if not allow_unvalidated:
            if manifest is None:
                manifest = CapabilityManifest(
                    supported_node_types=set(NodeType),
                    enabled_capabilities=set(),
                )
            result = WorkflowValidator.validate(revision.spec, manifest)
            if not result.valid:
                raise ValueError(
                    f"Cannot compile invalid workflow: {result.error_summary}"
                )

        return WorkflowCompiler._build_plan(revision)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_plan(revision: WorkflowRevision) -> ExecutionPlan:
        """Build the deterministic execution plan via topological sort."""
        spec = revision.spec
        nodes_by_id = {n.id: n for n in spec.nodes}

        # In-degree for topological sort.
        in_degree: dict[str, int] = {n.id: 0 for n in spec.nodes}
        successors: dict[str, list[tuple[str, WorkflowEdgeSpec]]] = {
            n.id: [] for n in spec.nodes
        }

        for edge in spec.edges:
            if edge.source_node_id not in nodes_by_id:
                continue
            if edge.target_node_id not in nodes_by_id:
                continue
            successors[edge.source_node_id].append((edge.target_node_id, edge))
            in_degree[edge.target_node_id] += 1

        # Kahn's algorithm with sorted ties for determinism.
        queue: deque[str] = deque(
            sorted([nid for nid, deg in in_degree.items() if deg == 0])
        )
        order: list[str] = []
        while queue:
            node_id = queue.popleft()
            order.append(node_id)
            ready: list[str] = []
            for target_id, _ in successors[node_id]:
                in_degree[target_id] -= 1
                if in_degree[target_id] == 0:
                    ready.append(target_id)
            for tid in sorted(ready):
                queue.append(tid)

        remaining = set(nodes_by_id.keys()) - set(order)
        if remaining:
            raise ValueError(
                f"Cannot compile workflow with cycle: remaining nodes {sorted(remaining)}"
            )

        # Build ExecutionStep for each node in topological order.
        steps: list[ExecutionStep] = []
        for idx, node_id in enumerate(order):
            nodes_by_id[node_id]
            input_mapping: dict[str, list[str]] = {}
            join_mode = "all"

            inbound = [e for e in spec.edges if e.target_node_id == node_id]
            for edge in inbound:
                src = f"{edge.source_node_id}:{edge.source_port}"
                if edge.target_port not in input_mapping:
                    input_mapping[edge.target_port] = []
                input_mapping[edge.target_port].append(src)

            if inbound:
                sources = {e.source_node_id for e in inbound}
                source_nodes = [nodes_by_id[s] for s in sources if s in nodes_by_id]
                if any(n.type == NodeType.CONDITION for n in source_nodes):
                    join_mode = "any"
                else:
                    join_mode = "all"

            outgoing_edges = [
                ExecutionEdgeInfo(
                    target_node_id=edge.target_node_id,
                    source_port=edge.source_port,
                    target_port=edge.target_port,
                    condition_value=edge.condition_value,
                )
                for edge in spec.edges
                if edge.source_node_id == node_id
            ]

            steps.append(ExecutionStep(
                node_id=node_id,
                order=idx,
                join_mode=join_mode,
                input_mapping=input_mapping,
                outgoing_edges=outgoing_edges,
            ))

        trigger_nodes = [n.id for n in spec.nodes if n.type == NodeType.TRIGGER]
        output_nodes = [n.id for n in spec.nodes if n.type == NodeType.OUTPUT]

        return ExecutionPlan(
            revision_hash=revision.content_hash,
            steps=steps,
            trigger_node_id=trigger_nodes[0] if trigger_nodes else "",
            output_node_ids=output_nodes,
        )
