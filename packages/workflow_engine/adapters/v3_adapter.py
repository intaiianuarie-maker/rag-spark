"""V3-backed NodeExecutor adapter skeleton (W3 / T-V4-015) — flagged OFF.

This is the *skeleton*: a structural ``NodeExecutor`` whose node execution
delegates to the V3 agents runtime, disabled by default so that no workflow
executes through it until the flag flips.

Adapter rules honoured (T-V4-015 card):

* **No direct v3 imports.** The V3 agents runtime is reached through an
  *injected* async callable (:data:`AgentRunner`) supplied by production
  wiring. This module imports nothing from ``packages/agents`` and adds zero
  new runtime dependencies.
* **Flag-off never dispatches.** ``V3NodeExecutor(enabled=False)`` refuses
  every node without touching the runner, so its ``dispatches`` counter stays
  at 0 — the metric the card names. :func:`resolve_node_executor` is the seam
  callers use: with the flag off it returns the existing default executor, so
  the default path is byte-identical (proven by a parity test that normalises
  only run identity and timing).
* **Missing capability → explicit failure.** A node whose
  ``required_capabilities`` are outside the manifest raises
  ``NotImplementedError`` — failure honesty, never mock success, mirroring the
  ``NodeExecutor`` Protocol contract and the W0/W1 fake runtime's behaviour.

Delegation discipline (the T-V3-011 seam pattern this ticket follows): the
adapter is a thin router, never an authority. It validates the flag and the
capability manifest locally, then hands the whole node request to the runner
and returns *exactly* what the runner returned — no local fabrication, no
transformation of the authority's output.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from workflow_engine.executor import FakeNodeExecutor
from workflow_engine.interfaces import CapabilityManifest, NodeExecutor, NodeSpec

#: Environment variable that enables the adapter. Default off: the flag must be
#: flipped deliberately, never inherited from an unset environment.
ADAPTER_FLAG_ENV = "WORKFLOW_V3_ADAPTER"

#: Values that count as enabled (case-insensitive, whitespace-trimmed).
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class V3AdapterDisabled(RuntimeError):
    """Raised when a node is routed to a disabled V3 adapter.

    Deliberately *not* a silent no-op: a disabled adapter that returned an
    empty mapping would look like a node that executed and found nothing,
    which is the false-success shape this package exists to prevent.
    """


@runtime_checkable
class AgentRunner(Protocol):
    """Narrow seam to the V3 agents runtime.

    Production wiring supplies an async callable that translates ``request``
    into a real agent run and returns that run's output-port mapping. Keeping
    this a Protocol (rather than importing ``packages.agents``) is what keeps
    the adapter free of direct v3 imports — a test spy satisfies it too.
    """

    async def __call__(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


def adapter_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Read the adapter flag from ``env`` (or ``os.environ``). Default False."""
    import os

    source = os.environ if env is None else env
    return source.get(ADAPTER_FLAG_ENV, "").strip().lower() in _TRUTHY


class V3NodeExecutor:
    """NodeExecutor that routes execution through the V3 agents runtime.

    Flagged off at construction: with ``enabled=False`` (the default) no node
    is ever dispatched and ``dispatches`` stays 0.
    """

    def __init__(
        self,
        *,
        agent_runner: AgentRunner,
        manifest: CapabilityManifest | None = None,
        enabled: bool = False,
    ) -> None:
        self._agent_runner = agent_runner
        self._manifest = manifest if manifest is not None else CapabilityManifest()
        self._enabled = bool(enabled)
        #: Number of nodes actually delegated to the V3 authority. The
        #: flag-off metric reads this: it must be 0 while disabled.
        self.dispatches: int = 0

    @property
    def enabled(self) -> bool:
        """Whether this adapter routes to V3. False by default."""
        return self._enabled

    @property
    def manifest(self) -> CapabilityManifest:
        """Capability manifest gating which nodes this adapter may route."""
        return self._manifest

    def _missing_capabilities(self, node: NodeSpec) -> list[str]:
        enabled = self._manifest.enabled_capabilities
        return [cap for cap in node.required_capabilities if cap not in enabled]

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any],
        workspace_id: str,
    ) -> dict[str, Any]:
        """Route *node* to the V3 agents runtime and return its outputs.

        Refuses (never fakes success) when the adapter is disabled, and when
        the node asks for a capability the manifest does not enable.
        """
        # Flag first: a disabled adapter refuses for its own reason, before any
        # capability or runner consultation — and before any dispatch.
        if not self._enabled:
            raise V3AdapterDisabled(
                "v3 workflow node execution is disabled "
                f"(set {ADAPTER_FLAG_ENV}=1 to enable): refused to route node "
                f"{node.id!r}"
            )

        # Missing capability is an explicit failure, not a downgrade.
        missing = self._missing_capabilities(node)
        if missing:
            raise NotImplementedError(
                "v3 adapter: node "
                f"{node.id!r} requires unavailable capabilities {missing} "
                "(available: "
                f"{sorted(self._manifest.enabled_capabilities)})"
            )

        # Delegate: hand the whole node request to the V3 authority unchanged.
        request: dict[str, Any] = {
            "node_id": node.id,
            "node_type": str(node.type),
            "node_config": dict(node.config),
            "workspace_id": workspace_id,
            "input_data": input_data,
            "required_capabilities": list(node.required_capabilities),
        }
        self.dispatches += 1
        return await self._agent_runner(request)


def resolve_node_executor(
    *,
    agent_runner: AgentRunner | None = None,
    manifest: CapabilityManifest | None = None,
    env: Mapping[str, str] | None = None,
    default_factory: Any = FakeNodeExecutor,
) -> NodeExecutor:
    """Seam: pick the node executor for this deployment.

    Flag off (default) → the existing default executor, so the default path is
    byte-identical and the adapter is never constructed or consulted.
    Flag on → :class:`V3NodeExecutor` wired to ``agent_runner``.

    Enabling without a runner is a configuration error rather than a silent
    fallback to the default: the caller asked for V3 and must supply the side
    that reaches it.
    """
    if not adapter_enabled(env):
        return default_factory()
    if agent_runner is None:
        raise V3AdapterDisabled(
            f"{ADAPTER_FLAG_ENV} is enabled but no agent_runner was supplied; "
            "refusing to fall back to the default executor"
        )
    return V3NodeExecutor(agent_runner=agent_runner, manifest=manifest, enabled=True)
