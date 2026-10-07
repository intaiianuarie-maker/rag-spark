"""LexicalStore contract (`retrieval.interfaces.LexicalStore`).

PostgreSQL FTS is the v1 lexical engine (REUSE_STRATEGY): the fake locks the
interface now, and the same checker runs against the real adapter once M3-001a
ships `retrieval.adapters.postgres_fts`.
"""

from __future__ import annotations

import importlib
import importlib.util
import os

import pytest
from harness_fakes import FakeLexicalStore
from retrieval.contract_tests import check_lexical_store_contract
from service_probe import require_service

pytestmark = [pytest.mark.unit, pytest.mark.contract]

FTS_MODULE = "retrieval.adapters.postgres_fts"
FTS_FACTORY = "create_lexical_store"
DEFAULT_DATABASE_URL = "postgresql+psycopg://knowledge:knowledge@localhost:5432/knowledge"


async def test_fake_lexical_store_satisfies_contract() -> None:
    """index / search / filter-by-workspace / delete round-trip (fake impl)."""
    await check_lexical_store_contract(FakeLexicalStore())


async def test_real_postgres_fts_adapter_satisfies_contract_when_implemented() -> None:
    """Loud skip until M3-001a; then the identical checker runs on live Postgres."""
    try:
        spec = importlib.util.find_spec(FTS_MODULE)
    except ModuleNotFoundError:
        spec = None
    if spec is None:
        pytest.skip(
            f"{FTS_MODULE} not implemented yet (M3-001a) — fake contract passed; "
            "the live-Postgres run belongs to that task (see tests/README.md)"
        )

    factory = getattr(importlib.import_module(FTS_MODULE), FTS_FACTORY, None)
    if not callable(factory):
        pytest.fail(f"{FTS_MODULE} must expose {FTS_FACTORY}() (see tests/README.md)")

    require_service(
        os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        "PostgreSQL",
        "docker compose up -d postgres",
    )
    store = factory()
    try:
        await check_lexical_store_contract(store)
    finally:
        # Caller-owned lifecycle, tolerant of the interface difference: the
        # `LexicalStore` ABC has no `close()` (only `VectorStore` does), so a
        # generic `await store.close()` would AttributeError for M3 adapters
        # that follow the ABC exactly.
        close = getattr(store, "close", None)
        if callable(close):
            await close()
