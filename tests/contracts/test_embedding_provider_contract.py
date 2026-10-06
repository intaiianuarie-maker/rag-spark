"""EmbeddingProvider contract (`retrieval.interfaces.EmbeddingProvider`).

Dimension + revision passthrough (PD-002: bge-m3, 1024-d, RO/PT/EN) is asserted
on the weight-free fake. The harness deliberately has **no** live-embedding test:
TASK-M0-004 forbids downloads of model weights and API keys, so the real
provider is checked with this same `check_embedding_provider_contract` by the
task that ships it (M3-001a / M7-001a).
"""

from __future__ import annotations

import pytest
from harness_fakes import FakeEmbeddingProvider
from retrieval.contract_tests import check_embedding_provider_contract

pytestmark = [pytest.mark.unit, pytest.mark.contract]


async def test_fake_embedding_provider_satisfies_contract() -> None:
    """Call shape, dimension match, determinism, empty-batch and invalid input."""
    await check_embedding_provider_contract(FakeEmbeddingProvider())


async def test_contract_requires_dimension_and_revision_metadata() -> None:
    """The seam the router depends on: model_name, revision and dimension."""
    provider = FakeEmbeddingProvider()
    assert provider.dimension == len(await provider.embed_query("probe"))
    assert provider.model_name and provider.revision
    assert provider.dimension > 0
