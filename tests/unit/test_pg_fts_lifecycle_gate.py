"""T-I03 — pg_fts must join the authoritative document/version lifecycle.

Review finding 3 (P1): ``pg_fts.search`` read ``lexical_docs`` with no document
tombstone / current-version join, so text belonging to a deleted or superseded
document stayed searchable between the tombstone commit and the FTS purge — and
forever if that purge failed.

The behavioural proofs run against the **real database**, because the claim under
test is about SQL semantics: the row must be excluded *by the query*, before
ranking, so it can never consume a ``top_k`` slot either. A canned session double
cannot demonstrate that, so it is used only where a fixture id that is not
UUID-shaped could never be inserted for real.

Structural assertions on the emitted SQL guard against a future edit silently
dropping the join.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, ClassVar

import pytest
from retrieval.adapters.pg_fts import (
    LIFECYCLE_JOINS,
    LIFECYCLE_PREDICATE,
    PostgresLexicalStore,
)
from retrieval.interfaces import SearchFilter

pytestmark = pytest.mark.unit

WS = str(uuid.uuid4())  # workspaces.id is a UUID column


def _normalise(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


async def _seed_lifecycle(
    session: Any,
    *,
    document_id: str,
    version_ids: list[str],
    deleted_at: str | None,
    current_version_id: str | None,
    superseded_versions: list[str],
    org_id: str,
) -> None:
    """Insert the authoritative documents / document_versions rows."""
    from sqlalchemy import text

    await session.execute(
        text(
            """
            INSERT INTO workspaces (id, organization_id, name, created_at, updated_at)
            VALUES (:ws, :org, :name, NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"ws": WS, "org": org_id, "name": "t-i03"},
    )
    await session.execute(
        text(
            """
            INSERT INTO documents
              (id, workspace_id, title, source_filename, mime_type,
               current_version_id, created_at, updated_at, deleted_at)
            VALUES (:id, :ws, :title, :fn, 'text/plain',
                    :cur, NOW(), NOW(), :deleted)
            """
        ),
        {
            "id": document_id,
            "ws": WS,
            "title": f"doc-{document_id[:8]}",
            "fn": f"{document_id}.txt",
            "cur": current_version_id,
            "deleted": deleted_at,
        },
    )
    for i, vid in enumerate(version_ids, start=1):
        await session.execute(
            text(
                """
                INSERT INTO document_versions
                  (id, document_id, version_number, size_bytes, checksum,
                   mime_type, created_at, superseded_at)
                VALUES (:id, :did, :n, :size, :chk, 'text/plain', NOW(), :sup)
                """
            ),
            {
                "id": vid,
                "did": document_id,
                "n": i,
                "size": 10,
                "chk": f"chk{i}-{vid[:8]}",
                "sup": "NOW()" if vid in superseded_versions else None,
            },
        )
    await session.commit()


async def _seed_lexical(session: Any, *, version_id: str, document_id: str, chunk_id: str,
                        content: str) -> None:
    """Insert the denormalised FTS row that outlives its document."""
    from sqlalchemy import text

    await session.execute(
        text(
            """
            INSERT INTO lexical_docs
              (id, workspace_id, document_id, document_version_id, chunk_id,
               content, language, metadata, tsv, tsv_cfg, page, section)
            VALUES
              (:id, :ws, :did, :dvid, :cid, :content, 'en',
               CAST(:meta AS jsonb),
               to_tsvector('english', :content), 'english', 1, NULL)
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "ws": WS,
            "did": document_id,
            "dvid": version_id,
            "cid": chunk_id,
            "content": content,
            "meta": json.dumps({}),
        },
    )
    await session.commit()


@pytest.fixture
async def session_factory():
    from ingestion.persistence import get_session_factory

    yield get_session_factory()
    store = PostgresLexicalStore()
    await store.delete(query_filter=SearchFilter(workspace_ids=[WS]))


async def _search_ws() -> list[Any]:
    store = PostgresLexicalStore()
    return await store.search(
        query="acquisition",
        top_k=10,
        query_filter=SearchFilter(workspace_ids=[WS]),
    )


# ---------------------------------------------------------------------------
# Behavioural proofs (real DB)
# ---------------------------------------------------------------------------


async def test_retained_tombstone_row_is_not_returned_or_ranked(session_factory) -> None:
    """Delayed cleanup: a row for a DELETED document must not surface.

    The FTS row is still physically present (the purge has not run, or failed) —
    so only the lifecycle join can exclude it.
    """
    from sqlalchemy import text

    async with session_factory() as s:
        org_id = str((await s.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one())

    doc = str(uuid.uuid4())
    ver = str(uuid.uuid4())
    async with session_factory() as s:
        await _seed_lifecycle(
            s, document_id=doc, version_ids=[ver], deleted_at="NOW()",
            current_version_id=ver, superseded_versions=[], org_id=org_id,
        )
        await _seed_lexical(s, version_id=ver, document_id=doc,
                            chunk_id="ch-tomb", content="the acquisition was in june")

    # Sanity: the row really is in the index and really is findable by content.
    async with session_factory() as s:
        raw = await s.execute(
            text("SELECT count(*) FROM lexical_docs WHERE document_id = :d"), {"d": doc}
        )
        assert int(raw.scalar_one()) == 1, "precondition: the stale row exists"

    items = await _search_ws()
    assert [i for i in items if i.chunk_id == "ch-tomb"] == [], (
        "a tombstoned document's text must never be a candidate"
    )


async def test_superseded_version_row_is_not_returned_or_ranked(session_factory) -> None:
    """A row for a SUPERSEDED version of a live document must not surface."""
    from sqlalchemy import text

    async with session_factory() as s:
        org_id = str((await s.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one())

    doc = str(uuid.uuid4())
    ver_old, ver_new = str(uuid.uuid4()), str(uuid.uuid4())
    async with session_factory() as s:
        await _seed_lifecycle(
            s, document_id=doc, version_ids=[ver_old, ver_new], deleted_at=None,
            current_version_id=ver_new, superseded_versions=[ver_old], org_id=org_id,
        )
        await _seed_lexical(s, version_id=ver_old, document_id=doc,
                            chunk_id="ch-sup", content="the acquisition was in march")

    items = await _search_ws()
    assert [i for i in items if i.chunk_id == "ch-sup"] == [], (
        "a superseded version's text must never be a candidate"
    )


async def test_live_row_still_returned_so_the_gate_does_not_over_block(
    session_factory,
) -> None:
    """The current version of a live document must still rank."""
    from sqlalchemy import text

    async with session_factory() as s:
        org_id = str((await s.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one())

    doc = str(uuid.uuid4())
    ver_old, ver_new = str(uuid.uuid4()), str(uuid.uuid4())
    async with session_factory() as s:
        await _seed_lifecycle(
            s, document_id=doc, version_ids=[ver_old, ver_new], deleted_at=None,
            current_version_id=ver_new, superseded_versions=[ver_old], org_id=org_id,
        )
        await _seed_lexical(s, version_id=ver_new, document_id=doc,
                            chunk_id="ch-live", content="the acquisition was in july")

    items = await _search_ws()
    live = [i for i in items if i.chunk_id == "ch-live"]
    assert len(live) == 1, "the live version must still be retrievable"
    assert live[0].method.value == "lexical"


# ---------------------------------------------------------------------------
# Structural guards (SQL text) — these hold regardless of DB state.
# ---------------------------------------------------------------------------


class _RecordingResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def mappings(self) -> _RecordingResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _Session:
    last_sql: ClassVar[str] = ""
    last_params: ClassVar[dict[str, Any]] = {}

    def __init__(self) -> None:
        self._rows: list[Any] = []

    async def execute(self, statement: Any, params: Any = None) -> _RecordingResult:
        type(self).last_sql = str(statement)
        type(self).last_params = dict(params or {})
        return _RecordingResult(self._rows)

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def _capture_sql() -> str:
    session = _Session()
    store = PostgresLexicalStore()
    store._session_factory = lambda: session  # type: ignore[method-assign]
    await store.search(
        query="acquisition", top_k=10, query_filter=SearchFilter(workspace_ids=[WS])
    )
    return _Session.last_sql


def test_lifecycle_predicate_covers_both_failure_paths() -> None:
    """The predicate must mention every authoritative lifecycle signal."""
    pred = _normalise(LIFECYCLE_PREDICATE)
    assert "d.deleted_at is null" in pred, "tombstone check missing"
    assert "d.current_version_id" in pred, "current-version check missing"
    assert "v.superseded_at is null" in pred, "superseded-generation check missing"
    assert "d.id is null" in pred, "no-claim pass-through rule missing"


def test_lifecycle_joins_are_left_joins_casting_uuid_to_text() -> None:
    """LEFT JOINs keep pre-migration rows eligible; never cast TEXT to UUID."""
    joins = _normalise(LIFECYCLE_JOINS)
    assert joins.count("left join") == 2
    assert "documents d on d.id::text = ld.document_id" in joins
    assert "document_versions v on v.id::text = ld.document_version_id" in joins
    assert "::uuid" not in joins, "casting a TEXT id to uuid can hard-fail a search"


async def test_emitted_sql_filters_before_ranking() -> None:
    """The lifecycle predicate must be in the WHERE, ahead of ORDER BY/LIMIT."""
    sql = _normalise(await _capture_sql())
    assert "left join documents" in sql
    assert "deleted_at is null" in sql
    assert "current_version_id" in sql
    assert "superseded_at is null" in sql
    where_at = sql.index("deleted_at is null")
    assert where_at < sql.index("order by"), "lifecycle gate must run before ranking"
    assert where_at < sql.index("limit"), "lifecycle gate must run before top_k"


async def test_acl_and_filters_survive_column_qualification() -> None:
    """Qualifying columns for the joins must not drop or widen the filters."""
    session = _Session()
    store = PostgresLexicalStore()
    store._session_factory = lambda: session  # type: ignore[method-assign]
    await store.search(
        query="acquisition",
        top_k=5,
        query_filter=SearchFilter(workspace_ids=[WS], language="en"),
    )
    sql = _normalise(_Session.last_sql)
    assert "ld.workspace_id = any(:wss)" in sql
    assert "ld.language = :lang" in sql
    assert _Session.last_params["wss"] == [WS]
    assert _Session.last_params["lang"] == "en"