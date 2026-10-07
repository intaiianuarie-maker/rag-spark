"""LexicalStore contract (`retrieval.interfaces.LexicalStore`).

PostgreSQL FTS is the v1 lexical engine (REUSE_STRATEGY): the fake locks the
interface now, and the same checker runs against the real adapter once the
rag-fork plan ships `retrieval.adapters.pg_fts` (legacy name
`retrieval.adapters.postgres_fts` also probed).
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

# Boss G1 ruling 2026-10-07: the rag-fork plan lands the factory-convention
# module as `adapters/pg_fts.py` (TASK-M3-003); the legacy harness name was
# `adapters.postgres_fts`. Probe both (tests/README.md convention table).
FTS_MODULES = ("retrieval.adapters.pg_fts", "retrieval.adapters.postgres_fts")
FTS_FACTORY = "create_lexical_store"


def _find_module(candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except ModuleNotFoundError:
            continue
    return None
DEFAULT_DATABASE_URL = "postgresql+psycopg://knowledge:knowledge@localhost:5432/knowledge"


async def test_fake_lexical_store_satisfies_contract() -> None:
    """index / search / filter-by-workspace / delete round-trip (fake impl)."""
    await check_lexical_store_contract(FakeLexicalStore())


async def test_real_postgres_fts_adapter_satisfies_contract_when_implemented() -> None:
    """Loud skip until the plan's adapter ships; then the identical checker
    runs on live Postgres (rag-fork TASK-M3-003 / superseded M3-001a)."""
    module = _find_module(FTS_MODULES)
    if module is None:
        pytest.skip(
            "none of " + ", ".join(FTS_MODULES) + " implemented yet "
            "(rag-fork TASK-M3-003) — fake contract passed; the live-Postgres run "
            "belongs to that task (see tests/README.md)"
        )

    factory = getattr(importlib.import_module(module), FTS_FACTORY, None)
    if not callable(factory):
        pytest.fail(f"{module} must expose {FTS_FACTORY}() (see tests/README.md)")

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
