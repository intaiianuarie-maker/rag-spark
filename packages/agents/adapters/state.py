"""StateGraph ↔ RunState adapter — skeleton (T-V3-011).

LangGraph's `StateGraph[StateT]` is generic; our `RunState`/`RunStatus` is
product-specific and frozen. This adapter embeds the product state inside the
execution state and makes `RunState.transition_to` the single authority —
adapter nodes call it, never duplicate its monotonic logic.

No `import langgraph` — the spike settled for wheel-inspection, and the
skeleton must not require the real package to import (BLOCKED otherwise).
"""

from __future__ import annotations

from typing import TypedDict

from ..interfaces import AgentRun, RunContext, RunState, RunStatus


class LangGraphState(TypedDict, total=False):
    """Execution state that embeds product state — the adapter's state shape."""

    run_state: RunState
    run_context: RunContext
    agent_run: AgentRun
    # LangGraph-internal bookkeeping (opaque to product code)
    messages: list[dict]
    next: str | None


class StateAdapter:
    """Adapter that hosts product `RunStatus` transitions inside a generic graph.

    The product's `RunState.transition_to` stays the single authority for
    monotonic terminal checks; this adapter only delegates to it.
    """

    @staticmethod
    def transition(state: RunState, target: RunStatus) -> None:
        """Delegate to the product authority — never duplicate the check."""
        state.transition_to(target)

    @staticmethod
    def is_terminal(status: RunStatus) -> bool:
        return RunState.is_terminal(status)

    @staticmethod
    def build_state(
        run_state: RunState,
        run_context: RunContext,
        agent_run: AgentRun,
    ) -> LangGraphState:
        """Construct the execution state that embeds the product state."""
        return LangGraphState(
            run_state=run_state,
            run_context=run_context,
            agent_run=agent_run,
            messages=[],
            next=None,
        )
