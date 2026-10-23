"""T-A03c proof tests: deletion/replacement fails through CanonicalChunkSource."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agents.canonical_source import CanonicalChunkSource


@dataclass
class _Hydrated:
    chunk_id: str
    content: str
    document_version_id: str | None = "v1"


class _Hydrator:
    def __init__(self, responses: list[dict[str, _Hydrated]]) -> None:
        self._responses = responses
        self._idx = 0

    async def hydrate(self, refs, *, allowed_workspace_ids=None, fresh=False):
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp


def test_delete_between_search_and_verify_fails() -> None:
    async def _run() -> None:
        hydrator = _Hydrator([{"chk-1": _Hydrated("chk-1", "canonical text")}, {}])
        source = CanonicalChunkSource(hydrator=hydrator)  # type: ignore[arg-type]

        assert await source.get_chunk_content("chk-1") == "canonical text"
        assert await source.get_chunk_content("chk-1") is None
        assert source.is_tombstoned("chk-1")

    asyncio.run(_run())


def test_replace_between_search_and_verify_fails() -> None:
    async def _run() -> None:
        hydrator = _Hydrator(
            [
                {"chk-1": _Hydrated("chk-1", "original", "v1")},
                {"chk-1": _Hydrated("chk-1", "replaced", "v2")},
            ]
        )
        source = CanonicalChunkSource(hydrator=hydrator)  # type: ignore[arg-type]

        assert await source.get_chunk_content("chk-1") == "original"
        assert await source.get_chunk_content("chk-1") is None
        assert source.is_tombstoned("chk-1")

    asyncio.run(_run())
