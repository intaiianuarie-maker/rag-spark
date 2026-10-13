"""Deterministic workflow compiler.

Compiles a validated ``WorkflowRevision`` into an ``ExecutionPlan``.
The compiler is pure: it produces a data structure describing execution
order and data-flow wiring.  It performs no I/O, no execution, and no
side effects.

Determinism (T-V4-008): the same definition bytes compile to a
byte-identical plan in every process.  Every collection the plan is built
from is either definition-ordered or explicitly sorted (no set iteration
reaches the plan), and the plan's ``revision_hash`` is seed-independent
(see ``WorkflowRevision._compute_content_hash``).

Join lock (T-V4-008): a node with two or more inbound edges is a join node
and must declare ``NodeSpec.join_strategy`` (``all`` / ``any`` / ``first``);
an undeclared join refuses to compile.  Each strategy carries its explicit
barrier on the step (``join_waits_for`` / ``join_emit_on_partial`` /
``join_timeout_s``).
"""

from __future__ import annotations

from collections import deque

from .interfaces import (
    CapabilityManifest,
    ExecutionEdgeInfo,
    ExecutionPlan,
    ExecutionStep,
    JoinStrategy,
    NodeSpec,
    NodeType,
    WorkflowEdgeSpec,
    WorkflowRevision,
)
from .validator import WorkflowValidator

#: Compile-time default join-barrier timeout (seconds).  The executor may
#: still be bounded by the run's own deadline; this is the barrier default
#: the plan states so the behaviour is explicit, not implicit.
JOIN_BARRIER_TIMEOUT_S = 30.0

#: The join barrier table — stated per strategy, not inferred:
#:   waits_for       — which inbound ports the node waits for
#:   emit_on_partial — what it emits while the barrier is incomplete
_JOIN_BARRIERS: dict[JoinStrategy, dict[str, str]] = {
    JoinStrategy.ALL: {
        "waits_for": "all-mapped",
        "emit_on_partial": "nothing",
    },
    JoinStrategy.ANY: {
        "waits_for": "all-mapped",
        "emit_on_partial": "first-arrival",
    },
    JoinStrategy.FIRST: {
        "waits_for": "first-mapped",
        "emit_on_partial": "first-input-only",
    },
}


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
    # T-V4-008: join lock — every join node declares its strategy
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_join_declarations(spec) -> None:
        """Refuse a join node whose strategy is undeclared.

        A node with two or more inbound edges is a join node: without a
        declared strategy the barrier is implicit, which is exactly the
        nondeterminism this lock closes.  Raises ``ValueError`` naming the
        node and the accepted strategies.
        """
        inbound_counts: dict[str, int] = {}
        for edge in spec.edges:
            inbound_counts[edge.target_node_id] = (
                inbound_counts.get(edge.target_node_id, 0) + 1
            )
        for node in spec.nodes:
            if inbound_counts.get(node.id, 0) < 2:
                continue  # not a join node: single wire (or none) needs no declared barrier
            if node.join_strategy is None:
                raise ValueError(
                    f"node '{node.id}' has {inbound_counts[node.id]} inbound "
                    f"edges but no declared join strategy; declare one of "
                    f"{[s.value for s in JoinStrategy]}"
                )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _join_barrier(node: NodeSpec, inbound: list[WorkflowEdgeSpec]) -> dict:
        """Build the stated barrier fields for one step (T-V4-008).

        Non-join nodes (fewer than two inbound edges) carry the trivial
        ``all`` barrier over their single mapped port.
        """
        ports = sorted({edge.target_port for edge in inbound})
        if len(inbound) < 2 or node.join_strategy is None:
            return {
                "join_mode": JoinStrategy.ALL.value,
                "join_waits_for": ports,
                "join_emit_on_partial": _JOIN_BARRIERS[JoinStrategy.ALL]["emit_on_partial"],
                "join_timeout_s": JOIN_BARRIER_TIMEOUT_S,
            }
        strategy = node.join_strategy
        barrier = _JOIN_BARRIERS[strategy]
        waits_for = (
            ports[:1] if barrier["waits_for"] == "first-mapped" else ports
        )
        return {
            "join_mode": strategy.value,
            "join_waits_for": waits_for,
            "join_emit_on_partial": barrier["emit_on_partial"],
            "join_timeout_s": JOIN_BARRIER_TIMEOUT_S,
        }

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_plan(revision: WorkflowRevision) -> ExecutionPlan:
        """Build the deterministic execution plan via topological sort."""
        spec = revision.spec
        nodes_by_id = {n.id: n for n in spec.nodes}

        # T-V4-008: the join lock runs before any plan construction.
        WorkflowCompiler._check_join_declarations(spec)

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
            node = nodes_by_id[node_id]
            inbound = [e for e in spec.edges if e.target_node_id == node_id]
            input_mapping: dict[str, list[str]] = {}
            for edge in inbound:
                src = f"{edge.source_node_id}:{edge.source_port}"
                input_mapping.setdefault(edge.target_port, []).append(src)

            # T-V4-008: the barrier is the node's DECLARED strategy (a join
            # node without one never reaches here — the lock above refused).
            # The inference from source node types is gone.
            barrier = WorkflowCompiler._join_barrier(node, inbound)

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
                join_mode=barrier["join_mode"],
                input_mapping={k: input_mapping[k] for k in sorted(input_mapping)},
                outgoing_edges=outgoing_edges,
                join_waits_for=barrier["join_waits_for"],
                join_emit_on_partial=barrier["join_emit_on_partial"],
                join_timeout_s=barrier["join_timeout_s"],
            ))

        trigger_nodes = [n.id for n in spec.nodes if n.type == NodeType.TRIGGER]
        output_nodes = [n.id for n in spec.nodes if n.type == NodeType.OUTPUT]

        return ExecutionPlan(
            revision_hash=revision.content_hash,
            steps=steps,
            trigger_node_id=trigger_nodes[0] if trigger_nodes else "",
            output_node_ids=output_nodes,
        )
