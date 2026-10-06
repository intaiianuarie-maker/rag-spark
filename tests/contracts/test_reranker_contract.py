"""Reranker contract (`retrieval.interfaces.Reranker`).

Weight-free fake only: reranking models are downloaded by the task that ships
the adapter (PD-002 selects bge-reranker-v2-m3), never by this harness. What is
locked here is the seam M3/M4 rely on: re-scored subset, descending order,
citation provenance preserved and the caller's list untouched.
"""

from __future__ import annotations

import pytest
from harness_fakes import FakeReranker, make_item
from retrieval.contract_tests import check_reranker_contract

pytestmark = [pytest.mark.unit, pytest.mark.contract]

DOCUMENTS = [
    make_item("hybrid retrieval uses dense and lexical signals", 0.9, page=1, section="Intro"),
    make_item("permissions are enforced before ranking", 0.8, page=2, section="ACL"),
    make_item("unrelated paragraph about lunch", 0.7, page=3, section="Other"),
    make_item("citations resolve to evidence ids", 0.6, page=4, section="Citations"),
]


async def test_fake_reranker_satisfies_contract() -> None:
    await check_reranker_contract(
        FakeReranker(),
        query="retrieval ranking citations",
        documents=DOCUMENTS,
        top_k=3,
    )


async def test_reranker_top_k_none_returns_everything() -> None:
    ranked = await FakeReranker().rerank(
        query="anything", documents=DOCUMENTS, top_k=None
    )
    assert len(ranked) == len(DOCUMENTS)


async def test_reranker_does_not_mutate_the_caller_list() -> None:
    before = [item.model_dump() for item in DOCUMENTS]
    await FakeReranker().rerank(query="ranking", documents=DOCUMENTS, top_k=2)
    assert [item.model_dump() for item in DOCUMENTS] == before
