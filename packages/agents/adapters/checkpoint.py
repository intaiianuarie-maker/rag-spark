"""BaseCheckpointSaver ↔ StepStore bridge — skeleton (T-V3-011).

Both `StepStore` (InMemory/SQLite, `append`/`list`/`get`, monotonic
`sequence == max+1`, redaction at `_prepare_event`, bounds
`MAX_EVENT_BYTES`/`MAX_SUMMARY_CHARS`/`MAX_REF_CHARS`, no `update`/`delete`)
and LangGraph's `BaseCheckpointSaver` (InMemory/Postgres, `put`/`get_tuple`/`list`,
`Checkpoint` with `channel_values`/`versions_seen`) are durable, ordered,
pluggable. This adapter makes `StepStore` the product log and `BaseCheckpointSaver`
the engine log, with `StepStore` staying append-only.

No `import langgraph` — the adapter is the seam, not the engine.
"""

from __future__ import annotations

from ..interfaces import RunContext, RunState, StepEvent, StepStatus, Usage
from ..step_store import StepStore


class StepStoreCheckpointAdapter:
    """Adapter that bridges `StepStore` ↔ `BaseCheckpointSaver` shape.

    Delegates to the product authority: `StepStore.append` (which already
    enforces monotonic `sequence`, redaction, and bounds) is the single
    place that writes. `get`/`list` delegate to `StepStore`. No `update`/
    `delete` surface is exposed — a step log that can be rewritten is not
    a step log.
    """

    def __init__(self, step_store: StepStore) -> None:
        self._store = step_store

    def save_checkpoint(self, run_id: str, event: StepEvent) -> StepEvent:
        """Delegate to `StepStore.append` — redaction + bounds + monotonic preserved."""
        return self._store.append(run_id, event)

    def load_checkpoint(self, run_id: str, sequence: int) -> StepEvent | None:
        return self._store.get(run_id, sequence)

    def list_checkpoints(self, run_id: str) -> list[StepEvent]:
        return self._store.list(run_id)

    # The adapter deliberately has no `update`/`delete` — append-only.
    # LangGraph's `BaseCheckpointSaver` has `put`/`put_writes`/`delete` in some
    # savers; this adapter does not expose `delete`/`update` even if the
    # underlying saver has it, preserving the product invariant.

    def as_step_store(self) -> StepStore:
        """Expose the underlying product `StepStore` — for conformance tests."""
        return self._store

    # For LangGraph's `BaseCheckpointSaver` shape, the adapter would implement
    # `put`/`get_tuple`/`list` by translating `Checkpoint` ↔ `StepEvent`.
    # Skeleton: types + delegation wiring, no behavior, no LangGraph import.
    def put(self, run_id: str, event: StepEvent) -> StepEvent:
        return self.save_checkpoint(run_id, event)

    def get_tuple(self, run_id: str, sequence: int) -> StepEvent | None:
        return self.load_checkpoint(run_id, sequence)

    def list_tuples(self, run_id: str) -> list[StepEvent]:
        return self.list_checkpoints(run_id)

    # T-V3-012: run checkpoint — durable via StepStore bridge, same redaction/bounds/monotonic
    def save_run_checkpoint(
        self,
        run_id: str,
        state: RunState,
        usage: Usage,
        context: RunContext,
        approvals: dict[tuple[str, str, str], str] | None = None,
        waiting: tuple[str, str, str, str] | None = None,
        agent_run: object | None = None,
    ) -> StepEvent:
        """Persist a run checkpoint through the StepStore bridge.

        Only `APPROVAL/WAITING` and terminal states are worth resuming from
        (ephemeral `RUNNING` steps stay in-memory). The checkpoint is a
        `StepEvent` with `action_summary` carrying the serialized run state,
        so the same redaction (`_prepare_event`) and bounds (`MAX_EVENT_BYTES`
        etc.) and monotonic `sequence` apply — no new table, no new surface.
        """
        import json
        from datetime import UTC, datetime

        # Build a checkpoint payload — typed, no hidden reasoning
        payload = {
            "run_state": state.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
            "run_context": context.model_dump(mode="json"),
            "approvals": {f"{k[0]}:{k[1]}:{k[2]}": v for k, v in (approvals or {}).items()},
            "waiting": waiting,
            "ts": datetime.now(UTC).isoformat(),
        }
        if agent_run is not None:
            try:
                # Store minimal AgentRun for trace equality — steps and total_tokens
                from ..interfaces import AgentRun

                if isinstance(agent_run, AgentRun):
                    payload["agent_run"] = agent_run.model_dump(mode="json")
                elif isinstance(agent_run, dict):
                    payload["agent_run"] = agent_run
            except Exception:
                pass
        # Use the next sequence for this run_id
        max_seq = 0
        try:
            # Peek max via list (works for both InMemory and SQLite)
            existing = self._store.list(run_id)
            if existing:
                max_seq = max(e.sequence for e in existing)
        except Exception:
            max_seq = 0
        seq = max_seq + 1
        # Encode as action_summary — bounded and redacted via _prepare_event
        # Use a compact JSON string, truncated by _prepare_event if needed
        summary = f"checkpoint:{json.dumps(payload, ensure_ascii=False, default=str)}"
        # Create a StepEvent that will be redacted/bounded/mono-checked by StepStore
        event = StepEvent(
            sequence=seq,
            step_id=f"ckpt-{seq}",
            attempt_id=f"ckpt-{run_id}-{seq}",
            action_summary=summary,
            tool_name=None,
            tool_input_ref=None,
            tool_output_ref=None,
            status=StepStatus.STARTED,
            latency_ms=0.0,
            usage=usage,
            timestamp=datetime.now(UTC).isoformat(),
        )
        # Delegate to the single authority: StepStore.append
        return self._store.append(run_id, event)

    def load_run_checkpoint(self, run_id: str) -> dict | None:
        """Load the latest run checkpoint for `run_id` — reconstruct state."""
        import json

        events = self._store.list(run_id)
        # Find the latest checkpoint event (action_summary starts with "checkpoint:")
        for ev in reversed(events):
            if ev.action_summary.startswith("checkpoint:"):
                try:
                    payload_str = ev.action_summary[len("checkpoint:"):]
                    payload = json.loads(payload_str)
                    # Re-hydrate RunState/Usage/RunContext
                    from ..interfaces import RunContext, RunState, Usage

                    state = RunState.model_validate(payload["run_state"])
                    usage = Usage.model_validate(payload["usage"])
                    context = RunContext.model_validate(payload["run_context"])
                    # approvals and waiting are optional
                    approvals_raw = payload.get("approvals") or {}
                    approvals = {tuple(k.split(":", 2)): v for k, v in approvals_raw.items()}
                    waiting = tuple(payload["waiting"]) if payload.get("waiting") else None
                    # Also try to load agent_run if present
                    agent_run_data = payload.get("agent_run")
                    agent_run_obj = None
                    if agent_run_data is not None:
                        try:
                            from ..interfaces import AgentRun

                            agent_run_obj = AgentRun.model_validate(agent_run_data)
                        except Exception:
                            agent_run_obj = None
                    return {
                        "state": state,
                        "usage": usage,
                        "context": context,
                        "approvals": approvals,
                        "waiting": waiting,
                        "event": ev,
                        "agent_run": agent_run_obj,
                    }
                except Exception:
                    continue
        return None
