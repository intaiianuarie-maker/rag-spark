"""Answer engine adapter boundaries (TASK-M4-002).

Only the seams the engine cannot own are declared here. The engine composes a
:class:`retrieval.interfaces.RetrievalOrchestrator` and a
:class:`models.interfaces.LLMProvider` (both injected), plus an optional
:class:`ChunkLookup` for store-backed existence checks.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Citation


@runtime_checkable
class ChunkLookup(Protocol):
    """Resolve a chunk id to citation provenance.

    Used to confirm a ``chunk_id`` exists in the store even when it was not in
    the retrieved top-k. When the engine is constructed without one, it verifies
    against the retrieved (already ACL-filtered) items only — the safe default.
    """

    async def get(self, chunk_id: str) -> Citation | None: ...
