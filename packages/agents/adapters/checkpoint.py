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

from ..interfaces import StepEvent
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
