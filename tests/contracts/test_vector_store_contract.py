"""VectorStore contract (`retrieval.interfaces.VectorStore`).

Runs the reusable checker from `packages/retrieval/contract_tests.py` against
the in-memory fake now and against the real Qdrant adapter once the rag-fork
plan ships `retrieval.adapters.qdrant_store` (legacy name
`retrieval.adapters.qdrant` also probed) — same assertions, so swapping the
implementation cannot weaken the
workspace filter or drop citation provenance (R-004, citation-spec).
"""

from __future__ import annotations

import importlib
import importlib.util
import os

import pytest
from harness_fakes import FakeVectorStore
from retrieval.contract_tests import CONTRACT_COLLECTION, check_vector_store_contract
from service_probe import DEFAULT_QDRANT_URL, require_service

pytestmark = [pytest.mark.unit, pytest.mark.contract]

# Boss G1 ruling 2026-10-07: the rag-fork plan lands the factory-convention
# module as `adapters/qdrant_store.py`; the legacy harness name was
# `adapters.qdrant`. Probe both so the hook activates on either and skips on
# neither-present (tests/README.md convention table).
QDRANT_MODULES = ("retrieval.adapters.qdrant_store", "retrieval.adapters.qdrant")
QDRANT_FACTORY = "create_vector_store"


def _find_module(candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except ModuleNotFoundError:
            continue
    return None


async def test_fake_vector_store_satisfies_contract() -> None:
    """insert / search / filter-by-workspace / delete round-trip (fake impl).

    Models the lifecycle ownership the checker documents: the test constructs
    the store and closes it; the checker only exercises it.
    """
    store = FakeVectorStore(dimension=4)
    try:
        await check_vector_store_contract(store)
    finally:
        await store.close()


async def test_real_qdrant_adapter_satisfies_contract_when_implemented() -> None:
    """Loud skip until the plan's adapter ships; then the identical checker
    runs on live Qdrant (rag-fork TASK-M3-001 / superseded M3-001a)."""
    module = _find_module(QDRANT_MODULES)
    if module is None:
        pytest.skip(
            "none of " + ", ".join(QDRANT_MODULES) + " implemented yet "
            "(rag-fork TASK-M3-001) — fake contract passed; the live-Qdrant run "
            "belongs to that task (see tests/README.md)"
        )

    factory = getattr(importlib.import_module(module), QDRANT_FACTORY, None)
    if not callable(factory):
        pytest.fail(
            f"{module} must expose {QDRANT_FACTORY}() -> VectorStore "
            "(harness convention, tests/README.md)"
        )
    require_service(
        os.environ.get("QDRANT_URL", DEFAULT_QDRANT_URL),
        "Qdrant",
        "docker compose up -d qdrant",
    )
    store = factory()
    try:
        await check_vector_store_contract(store, collection=f"{CONTRACT_COLLECTION}-m004")
    finally:
        # Sole close: the checker no longer closes the store (code1 review
        # Minor 1 — lifecycle ownership is the caller's).
        await store.close()
