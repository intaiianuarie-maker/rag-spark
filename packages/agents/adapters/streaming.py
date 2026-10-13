"""Streaming ↔ StepEvent — skeleton (T-V3-011).

Our `StepEvent` (sequence, step_id, attempt_id, action_summary, tool_*_ref,
status, error_code, latency_ms, usage, timestamp — no hidden reasoning) vs
LangGraph's `StreamMode` (values/updates/checkpoints/tasks/debug/messages/custom)
+ `StreamWriter` + `StreamPart`. The adapter keeps `StepEvent` as the product
stream and `StreamMode="custom"` as the engine stream.

No `import langgraph` — the adapter is the seam.
"""

from __future__ import annotations

from typing import Any

from ..interfaces import StepEvent


class StreamingAdapter:
    """Adapter that bridges product `StepEvent` ↔ LangGraph `StreamMode`."""

    @staticmethod
    def to_custom_part(event: StepEvent) -> dict[str, Any]:
        """Translate product `StepEvent` to LangGraph `CustomStreamPart` shape (stub)."""
        return {"type": "custom", "event": event.model_dump()}

    @staticmethod
    def from_custom_part(data: dict[str, Any]) -> StepEvent:
        """Translate LangGraph `CustomStreamPart` back to product `StepEvent` (stub)."""
        event_data = data.get("event") or data
        return StepEvent.model_validate(event_data)

    @staticmethod
    def is_hidden_reasoning_exposed(stream_mode: str) -> bool:
        """Check if a LangGraph `StreamMode` would expose hidden reasoning."""
        # Only "custom" with StepEvent is safe; "messages" would stream raw LLM tokens
        return stream_mode == "messages"

    @staticmethod
    def check_no_hidden_reasoning(event: StepEvent) -> bool:
        """Prove by construction: StepEvent has no reasoning field."""
        fields = set(StepEvent.model_fields.keys())
        return not bool(fields & {"reasoning", "chain_of_thought", "thought", "hidden_reasoning"})
