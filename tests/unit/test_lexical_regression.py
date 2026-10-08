"""Permanent regression tests for PG FTS Majors 3/4/5 (M3-003 rework).

Probe-covered in test1's 17/17, now landed as small permanent tests:
- cross-language recall without language filter hits RO+PT
- metadata key injection is rejected pre-SQL
- delete rowcount is honest for mixed ids and repeat filter-delete
"""

from __future__ import annotations

import pytest
from retrieval.adapters.pg_fts import PostgresLexicalStore
from retrieval.interfaces import SearchFilter, VectorRecord

pytestmark = pytest.mark.unit


def _rec(
    id: str, content: str, ws: str, lang: str | None = None, meta: dict | None = None
) -> VectorRecord:
    m = dict(meta or {})
    if lang:
        m["language"] = lang
    return VectorRecord(
        id=id, vector=[0, 0, 0, 0], content=content, workspace_id=ws, chunk_id=id, metadata=m
    )


async def test_cross_language_no_filter_hits_ro_and_pt() -> None:
    store = PostgresLexicalStore()
    await store.delete(query_filter=SearchFilter(workspace_ids=["ws-lang"]))
    await store.index(
        records=[
            _rec("lang-ro", "bună ziua", "ws-lang", lang="ro"),
            _rec("lang-pt", "olá mundo", "ws-lang", lang="pt"),
        ]
    )
    # No language filter → both should be found via per-row tsv_cfg
    hits_ro = await store.search(
        query="bună", top_k=10, query_filter=SearchFilter(workspace_ids=["ws-lang"])
    )
    hits_pt = await store.search(
        query="olá", top_k=10, query_filter=SearchFilter(workspace_ids=["ws-lang"])
    )
    assert any(h.chunk_id == "lang-ro" or h.content == "bună ziua" for h in hits_ro) or any(
        "bună" in h.content for h in hits_ro
    )
    assert any("olá" in h.content for h in hits_pt)
    # With language filter, still hits the matching one
    hits_ro_f = await store.search(
        query="bună", top_k=10, query_filter=SearchFilter(workspace_ids=["ws-lang"], language="ro")
    )
    assert len(hits_ro_f) == 1
    await store.delete(query_filter=SearchFilter(workspace_ids=["ws-lang"]))


async def test_metadata_key_injection_rejected() -> None:
    store = PostgresLexicalStore()
    with pytest.raises(ValueError, match="metadata key not allowed"):
        await store.index(records=[_rec("inj", "x", "ws-inj", meta={"bad-key!": "val"})])
    bad_meta = {"bad-key!": "val"}
    with pytest.raises(ValueError, match="metadata key not allowed"):
        await store.search(
            query="x",
            top_k=10,
            query_filter=SearchFilter(workspace_ids=["ws-inj"], metadata=bad_meta),
        )


async def test_delete_rowcount_honest() -> None:
    store = PostgresLexicalStore()
    await store.delete(query_filter=SearchFilter(workspace_ids=["ws-del"]))
    await store.index(
        records=[
            _rec("del-a", "hello", "ws-del"),
            _rec("del-b", "hello", "ws-del"),
        ]
    )
    n1 = await store.delete(ids=["del-a", "ghost1", "ghost2"])
    assert n1 == 1
    # Filter delete honest: first removes 1, second on empty scope is 0
    n2 = await store.delete(query_filter=SearchFilter(workspace_ids=["ws-del"]))
    assert n2 == 1
    n3 = await store.delete(query_filter=SearchFilter(workspace_ids=["ws-del"]))
    assert n3 == 0
    await store.delete(query_filter=SearchFilter(workspace_ids=["ws-del"]))
