"""Authoritative, workspace-scoped chunk source for research verification (T-A03).

Wraps ``answer.hydration.CanonicalHydrator`` without editing it. The hydrator
already enforces the authoritative rules: it resolves refs through the canonical
store, refuses deleted/superseded document versions, and scopes the result to
the caller's workspace. This source surfaces that as the ``ChunkSource``
Protocol the verifier uses, so candidate text carried by retrieval never
authenticates a claim — only the current canonical store row can.

Every read is ``fresh=True`` so a delete or version replace that lands between
search and verification is observed (and fails the claim). A baseline signature
is recorded on first read; any subsequent read whose signature differs is
treated as a change (tombstoned), so replacement between search and verify also
fails.
"""

from __future__ import annotations

from typing import Any

from answer.hydration import CanonicalHydrator
from answer.interfaces import ChunkLookup

_UNSET: object = object()


class CanonicalChunkSource:
    """ChunkSource backed by the canonical PostgreSQL hydrator."""

    def __init__(
        self,
        hydrator: CanonicalHydrator | None = None,
        *,
        lookup: ChunkLookup | None = None,
        allowed_workspace_ids: set[str] | None = None,
    ) -> None:
        if hydrator is None:
            from answer.adapters.postgres_lookup import PostgresChunkLookup

            hydrator = CanonicalHydrator(lookup or PostgresChunkLookup())
        self._hydrator = hydrator
        self._allowed_workspace_ids = allowed_workspace_ids
        self._baselines: dict[str, str] = {}
        self._tombstoned: set[str] = set()

    async def get_chunk_content(self, chunk_id: str) -> str | None:
        workspace_ids = self._allowed_workspace_ids
        if workspace_ids == set():
            workspace_ids = None
        try:
            hydrated = await self._hydrator.hydrate(
                [chunk_id],
                allowed_workspace_ids=workspace_ids,
                fresh=True,
            )
        except Exception:
            return None
        if chunk_id not in hydrated:
            self._tombstoned.add(chunk_id)
            return None
        chunk = hydrated[chunk_id]
        content = chunk.content
        if content is None:
            self._tombstoned.add(chunk_id)
            return None
        signature = f"{getattr(chunk, 'document_version_id', None)}:{content}"
        prior = self._baselines.get(chunk_id, _UNSET)
        if prior is not _UNSET and prior != signature:
            self._tombstoned.add(chunk_id)
            return None
        self._baselines[chunk_id] = signature
        return content

    def is_tombstoned(self, chunk_id: str) -> bool:
        return chunk_id in self._tombstoned

    def put(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - no-op
        """Refuse to authenticate candidate text; kept as a no-op for protocol symmetry."""
        return None
