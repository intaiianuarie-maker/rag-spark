"""VectorStore contract (`retrieval.interfaces.VectorStore`).

Runs the reusable checker from `packages/retrieval/contract_tests.py` against
the in-memory fake now and against the real Qdrant adapter once M3-001a ships
one — same assertions, so swapping the implementation cannot weaken the
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

QDRANT_MODULE = "retrieval.adapters.qdrant"
QDRANT_FACTORY = "create_vector_store"


async def test_fake_vector_store_satisfies_contract() -> None:
    """insert / search / filter-by-workspace / delete round-trip (fake impl)."""
    await check_vector_store_contract(FakeVectorStore(dimension=4))


async def test_real_qdrant_adapter_satisfies_contract_when_implemented() -> None:
    """Loud skip until M3-001a; then the identical checker runs on live Qdrant."""
    try:
        spec = importlib.util.find_spec(QDRANT_MODULE)
    except ModuleNotFoundError:
        spec = None
    if spec is None:
        pytest.skip(
            f"{QDRANT_MODULE} not implemented yet (M3-001a) — fake contract passed; "
            "the live-Qdrant run belongs to that task (see tests/README.md)"
        )

    factory = getattr(importlib.import_module(QDRANT_MODULE), QDRANT_FACTORY, None)
    if not callable(factory):
        pytest.fail(
            f"{QDRANT_MODULE} must expose {QDRANT_FACTORY}() -> VectorStore "
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
        await store.close()
