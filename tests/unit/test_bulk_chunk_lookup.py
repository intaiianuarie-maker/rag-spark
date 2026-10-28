"""T-V1-047 — bulk ``get_many`` on the chunk lookup (kill the N+1 reads).

Hydration used to resolve a candidate batch with **one single-row query per
ref** — ~16 round-trips per ``/ask``, each opening its own session. The
``ChunkLookup`` contract now carries a bulk seam, the Postgres adapter answers
a whole batch with one ``IN (…)`` query, and
:meth:`answer.hydration.CanonicalHydrator.hydrate` issues a single store pass
for every ref it still has to read.

Pinned here, service-free (an in-file stub session + stub session factory; no
Postgres, no Redis, no Qdrant):

1. **The metric** — 16 ids ⇒ exactly **one** ``session.execute``.
2. **Identical semantics** — bulk output equals per-id ``get`` output for the
   same ids, including the deleted / superseded / missing / foreign cases.
3. **No scope loosening** — the caller's workspace refusal and the lifecycle
   refusal are unchanged on the bulk path, proven through the real adapter.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from answer.adapters.postgres_lookup import MemoryChunkLookup, PostgresChunkLookup
from answer.hydration import CanonicalHydrator, CanonicalStoreUnavailable

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.unit

WS = "ws-a"
FOREIGN_WS = "ws-b"


def _uid(n: int) -> str:
    """A real UUID string — the adapter validates chunk-id shape."""
    return str(uuid.UUID(int=n))


# --------------------------------------------------------------------------------------
# Stub session — serves rows, counts every statement it is asked to execute
# --------------------------------------------------------------------------------------


class _Row(SimpleNamespace):
    """One ``chunks ⋈ document_versions ⋈ documents`` result row."""


def _row(
    chunk_id: str,
    *,
    workspace_id: str = WS,
    deleted_at: str | None = None,
    superseded_at: str | None = None,
    text: str = "canonical passage",
) -> _Row:
    return _Row(
        id=chunk_id,
        workspace_id=workspace_id,
        document_id="doc-1",
        document_version_id="ver-1",
        text=text,
        page=1,
        section="Budget",
        source_filename="launch_plan.pdf",
        deleted_at=deleted_at,
        superseded_at=superseded_at,
    )


class _Result:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def first(self) -> _Row | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[_Row]:
        return list(self._rows)


class _Session:
    """Enough of an ``AsyncSession`` to exercise both lookup paths offline."""

    def __init__(self, rows_by_id: dict[str, _Row]) -> None:
        self._rows_by_id = rows_by_id
        #: Every ``(sql, params)`` this session was asked to execute.
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, stmt: Any, params: dict[str, Any]) -> _Result:
        self.execute_calls.append((str(stmt), dict(params)))
        # Branch on the params, not on rendered SQL: an expanding bindparam
        # renders as ``__[POSTCOMPILE_chunk_ids]``, not as ``:chunk_ids``.
        if "chunk_ids" in params:
            ids = params["chunk_ids"]
            return _Result([self._rows_by_id[i] for i in ids if i in self._rows_by_id])
        chunk_id = params["chunk_id"]
        return _Result([self._rows_by_id[chunk_id]] if chunk_id in self._rows_by_id else [])

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _BrokenSession:
    """A store that is down: every statement raises."""

    async def execute(self, stmt: Any, params: dict[str, Any]) -> _Result:
        raise RuntimeError("postgres down")

    async def __aenter__(self) -> _BrokenSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _SessionFactory:
    """Called as ``factory()`` → an async context manager yielding a session."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return self._session


def _lookup(rows: dict[str, _Row]) -> tuple[PostgresChunkLookup, _Session]:
    session = _Session(rows)
    return PostgresChunkLookup(session_factory=_SessionFactory(session)), session


# --------------------------------------------------------------------------------------
# 1. The metric: one query for the whole batch
# --------------------------------------------------------------------------------------


async def test_sixteen_ids_cost_exactly_one_query() -> None:
    """Reviewer checklist (2): the N+1 is gone — 16 ids ⇒ 1 execute."""
    ids = [_uid(i) for i in range(16)]
    lookup, session = _lookup({cid: _row(cid) for cid in ids})

    resolved = await lookup.get_many(ids)

    assert len(resolved) == 16
    assert len(session.execute_calls) == 1, "one bulk fetch, not one query per id"
    sql, params = session.execute_calls[0]
    # An expanding bindparam renders as ``__[POSTCOMPILE_chunk_ids]``; what
    # matters is that ONE statement carries the whole id list.
    assert "chunk_ids" in sql, "the batch must be one IN-list statement"
    assert params["chunk_ids"] == ids


async def test_the_session_is_opened_once_for_the_batch() -> None:
    """Not just one statement: one session, too (was one open per ref)."""
    ids = [_uid(i) for i in range(8)]
    session = _Session({cid: _row(cid) for cid in ids})
    factory = _SessionFactory(session)
    lookup = PostgresChunkLookup(session_factory=factory)

    await lookup.get_many(ids)

    assert factory.calls == 1


# --------------------------------------------------------------------------------------
# 2. Identical semantics to the per-id path
# --------------------------------------------------------------------------------------


async def test_bulk_output_equals_per_id_output() -> None:
    """Reviewer checklist (1): bulk ≡ per-id, refusal cases included."""
    live, deleted, superseded, foreign = _uid(1), _uid(2), _uid(3), _uid(4)
    missing = _uid(99)
    rows = {
        live: _row(live),
        deleted: _row(deleted, deleted_at="2026-01-01T00:00:00"),
        superseded: _row(superseded, superseded_at="2026-01-01T00:00:00"),
        foreign: _row(foreign, workspace_id=FOREIGN_WS),
    }
    ids = [live, deleted, superseded, foreign, missing]
    lookup, _ = _lookup(rows)

    bulk = await lookup.get_many(ids)
    per_id: dict[str, Any] = {}
    for chunk_id in ids:
        citation = await lookup.get(chunk_id)
        if citation is not None:
            per_id[chunk_id] = citation

    assert bulk == per_id, "bulk must not widen or narrow the per-id result"
    assert set(bulk) == {live, foreign}, "deleted + superseded + missing refused"
    assert bulk[foreign].content == "canonical passage"


async def test_every_row_carries_the_same_provenance_as_a_single_read() -> None:
    """The bulk path must not drop page/section/filename/workspace."""
    cid = _uid(7)
    lookup, _ = _lookup({cid: _row(cid, text="budget is 20")})

    citation = (await lookup.get_many([cid]))[cid]

    single = await lookup.get(cid)
    assert citation.model_dump() == single.model_dump()
    assert (citation.page, citation.section, citation.filename) == (1, "Budget", "launch_plan.pdf")
    assert citation.content == "budget is 20"


async def test_duplicate_ids_are_collapsed_to_one_read() -> None:
    ids = _uid(5)
    lookup, session = _lookup({ids: _row(ids)})

    resolved = await lookup.get_many([ids, ids, ids])

    assert list(resolved) == [ids]
    assert session.execute_calls[0][1]["chunk_ids"] == [ids], "deduped before the query"


async def test_memory_lookup_bulk_matches_per_id_get() -> None:
    """The in-memory adapter keeps identical semantics too."""
    lookup = MemoryChunkLookup()
    lookup.add_chunk("ch-1", content="alpha", workspace_id=WS)
    lookup.add_chunk("ch-dead", content="x", workspace_id=WS, is_deleted=True)
    lookup.add_chunk("ch-super", content="x", workspace_id=WS, is_superseded=True)
    ids = ["ch-1", "ch-dead", "ch-super", "ch-missing"]

    bulk = await lookup.get_many(ids)
    per_id = {i: c for i in ids if (c := await lookup.get(i)) is not None}

    assert bulk == per_id
    assert set(bulk) == {"ch-1"}


# --------------------------------------------------------------------------------------
# 3. Boundaries: shape validation, empty batches, a store that is down
# --------------------------------------------------------------------------------------


async def test_invalid_ids_are_dropped_and_never_reach_the_query() -> None:
    """Same shape rule as ``get``: a non-UUID can never name a row."""
    live = _uid(1)
    lookup, session = _lookup({live: _row(live)})

    resolved = await lookup.get_many(["not-a-uuid", "", live, "123"])

    assert set(resolved) == {live}
    assert session.execute_calls[0][1]["chunk_ids"] == [live]


async def test_an_empty_batch_issues_no_query_at_all() -> None:
    lookup, session = _lookup({})

    assert await lookup.get_many([]) == {}
    assert await lookup.get_many(["not-a-uuid", "also bad"]) == {}
    assert session.execute_calls == [], "nothing to read ⇒ no round-trip"


async def test_a_store_failure_fails_closed() -> None:
    """An outage raises, so it is never mistaken for "not found" or a partial batch."""
    lookup = PostgresChunkLookup(session_factory=_SessionFactory(_BrokenSession()))

    with pytest.raises(CanonicalStoreUnavailable):
        await lookup.get_many([_uid(1), _uid(2)])


# --------------------------------------------------------------------------------------
# 4. The call site: one store pass per hydrate, scope refusal unchanged
# --------------------------------------------------------------------------------------


class _CountingMemory(MemoryChunkLookup):
    """Records how the hydrator talks to the store."""

    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        super().__init__(records)
        self.bulk_calls = 0
        self.single_calls = 0

    async def get(self, chunk_id: str):
        self.single_calls += 1
        return await super().get(chunk_id)

    async def get_many(self, chunk_ids: Any):
        self.bulk_calls += 1
        return await super().get_many(chunk_ids)


async def test_hydrator_resolves_sixteen_refs_in_one_store_pass() -> None:
    """The call site: 16 refs ⇒ 1 store pass, and the lookups metric stands."""
    lookup = _CountingMemory()
    ids = [f"ch-{i:02d}" for i in range(16)]
    for chunk_id in ids:
        lookup.add_chunk(chunk_id, content=f"canonical {chunk_id}", workspace_id=WS)
    lookup.add_chunk("ch-foreign", content="secret", workspace_id=FOREIGN_WS)
    hydrator = CanonicalHydrator(lookup)

    resolved = await hydrator.hydrate(["ch-foreign", *ids], allowed_workspace_ids={WS})

    assert lookup.bulk_calls == 1, "one bulk fetch for the whole batch"
    assert len(resolved) == 16
    assert hydrator.lookups == 17  # refs read: 16 in-scope + 1 scope-refused


async def test_a_second_pass_is_served_from_cache() -> None:
    """A repeat hydrate must not buy a second store pass."""
    lookup = _CountingMemory()
    lookup.add_chunk("ch-1", content="canonical", workspace_id=WS)
    hydrator = CanonicalHydrator(lookup)

    first = await hydrator.hydrate(["ch-1"], allowed_workspace_ids={WS})
    second = await hydrator.hydrate(["ch-1"], allowed_workspace_ids={WS})

    assert set(first) == set(second) == {"ch-1"}
    assert lookup.bulk_calls == 1, "the second pass is a cache hit"
    assert hydrator.lookups == 1


async def test_bulk_hydration_still_refuses_foreign_and_lifecycle_rows() -> None:
    """Reviewer checklist (3): no scope loosening through the real adapter."""
    in_scope, foreign, dead = _uid(1), _uid(2), _uid(3)
    rows = {
        in_scope: _row(in_scope),
        foreign: _row(foreign, workspace_id=FOREIGN_WS),
        dead: _row(dead, deleted_at="2026-01-01T00:00:00"),
    }
    lookup, session = _lookup(rows)
    hydrator = CanonicalHydrator(lookup)

    resolved = await hydrator.hydrate(
        [in_scope, foreign, dead], allowed_workspace_ids={WS}
    )

    assert set(resolved) == {in_scope}, "foreign + deleted stay refused on the bulk path"
    assert resolved[in_scope].content == "canonical passage"
    assert len(session.execute_calls) == 1


async def test_sixteen_refs_through_the_real_adapter_cost_one_query() -> None:
    """The headline metric, composed: hydrator + the real Postgres adapter.

    Before T-V1-047 this exact path issued one single-row query (and opened
    one session) per ref — ~16 round-trips per ``/ask``. The same 16 refs now
    cost a single execute, with the same 16 canonical records out.
    """
    ids = [_uid(i) for i in range(16)]
    lookup, session = _lookup({cid: _row(cid) for cid in ids})
    hydrator = CanonicalHydrator(lookup)

    resolved = await hydrator.hydrate(ids, allowed_workspace_ids={WS})

    assert len(resolved) == 16, "every ref must still resolve to its canonical record"
    assert len(session.execute_calls) == 1, "~16 round-trips per /ask -> 1"
    assert hydrator.lookups == 16  # refs read: the T-V1-022 metric is unchanged
