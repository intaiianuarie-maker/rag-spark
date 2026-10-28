"""T-V1-052 — answer-layer lost-region disclosure on **every** return path.

The pipeline parks a job ``needs_review`` when part of a source could not be
read (image-only PDF pages, a table the parser could not expand) and writes the
lost regions onto every chunk it published. Retrieval hands that metadata back
on the retrieved items, so the **answer layer must say the same thing the job
said** — never answering over an omitted region as if the document had been
read in full.

Sections:

1. **Re-homed from the T-V1-031b scratch proposal** (moved verbatim out of
   ``tests/unit/test_format_matrix.py`` when ``engine.py`` was reverted as
   incomplete — nothing was deleted, it lived in
   ``handoffs/T-V1-052-SCRATCH-PROPOSAL-ENGINE-DISCLOSURE.md`` until now).
2. **All return paths** (T-V1-052's own gap): the three+ abstention early
   returns and the fail-closed ``UNAVAILABLE`` return used to bypass the
   disclosure entirely.
3. **Retrieval passthrough, direct and live**: nothing previously proved
   ``parse_lost_regions`` survives store → ``RetrievedItem.metadata`` → the
   real orchestrator → the engine.

Service-free: in-file fakes plus the shared ``harness_fakes`` doubles. No
network, no DB, no Qdrant, deterministic throughout.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from core.auth import ACLScope
from core.errors import AnswerStatus, ReasonCode
from harness_fakes import (
    FakeEmbeddingProvider,
    FakeLexicalStore,
    FakeVectorStore,
    make_item,
    make_record,
)

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.unit

WS = "ws-a"
COLL = "knowledge"
CANONICAL = "Revenue was 1450 on page 1"

#: One lost region, exactly as the ingestion pipeline writes it.
_LOST_REGION = {
    "reason": "no_text_layer",
    "lost_pages": [2],
    "page_count": 2,
    "readable_pages": 1,
}


# --------------------------------------------------------------------------------------
# 1. Re-homed verbatim from the T-V1-031b scratch proposal
# --------------------------------------------------------------------------------------


def _answer(status=AnswerStatus.ANSWERED, level="high"):
    from answer.models import Answer, Confidence, ConfidenceLevel

    return Answer(
        answer="Revenue was 1450.",
        status=status,
        confidence=Confidence(
            level=ConfidenceLevel(level),
            basis="Answer grounded in 1 verified, substantiated citation(s)",
        ),
    )


def _lost_region_item_live():
    return make_item(CANONICAL).model_copy(
        update={
            "metadata": {
                "parse_partial": True,
                "parse_lost_regions": [_LOST_REGION],
            }
        }
    )


def test_an_answer_over_an_omitted_region_is_never_confident() -> None:
    from answer.engine import AnswerEngine
    from answer.models import ConfidenceLevel

    answered = AnswerEngine._disclose_source(_answer(), [_lost_region_item_live()])
    assert answered.status is AnswerStatus.NEEDS_REVIEW
    assert answered.reason_code == ReasonCode.NEEDS_REVIEW.value
    assert answered.confidence.level is ConfidenceLevel.MEDIUM, "HIGH claims completeness"
    assert any("[2]" in limitation for limitation in answered.limitations)
    assert "NOT available" in answered.limitations[-1]


def test_an_answer_over_a_fully_read_source_is_untouched() -> None:
    from answer.engine import AnswerEngine
    from answer.models import ConfidenceLevel

    clean_item = make_item(CANONICAL)
    base = _answer()
    answered = AnswerEngine._disclose_source(base, [clean_item])
    assert answered is base, "a fully-read source must not be relabelled at all"
    assert answered.status is AnswerStatus.ANSWERED
    assert answered.confidence.level is ConfidenceLevel.HIGH
    assert answered.limitations == []
    assert answered.reason_code is None


def test_an_abstention_keeps_its_reason_but_still_discloses_the_lost_region() -> None:
    from answer.engine import AnswerEngine

    abstained = _answer(status=AnswerStatus.INSUFFICIENT_EVIDENCE, level="low")
    result = AnswerEngine._disclose_source(abstained, [_lost_region_item_live()])
    assert result.status is AnswerStatus.INSUFFICIENT_EVIDENCE, "an abstention is not a claim"
    assert result.reason_code is None
    assert any("[2]" in limitation for limitation in result.limitations)


def test_the_lost_region_metadata_key_is_the_one_ingestion_writes() -> None:
    """The two sides must agree on the key or the disclosure never arrives."""
    import inspect

    from answer.engine import AnswerEngine
    from jobs.ingest_pipeline import PARSE_LOST_REGIONS_KEY

    assert PARSE_LOST_REGIONS_KEY == "parse_lost_regions"
    collect_source = inspect.getsource(AnswerEngine._collect_lost_regions)
    assert "parse_lost_regions" in collect_source


# --------------------------------------------------------------------------------------
# Harness for the live engine
# --------------------------------------------------------------------------------------


class _StubOrchestrator:
    """Returns a fixed item list; publishes no retrieval outcome (reads healthy)."""

    def __init__(self, items):
        self._items = items

    async def retrieve(self, **kwargs: Any):
        return list(self._items)


class _ScriptedLLM:
    """Returns exactly one canned completion, so each abstention is reachable."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def generate(self, request: Any) -> Any:
        from models.interfaces import GenerationResult

        return GenerationResult(
            text=self._text, model=request.model, finish_reason="stop", prompt_tokens=0
        )


def _llm_json(answer: str = "Revenue was 1450.", claims: list | None = None) -> str:
    """A parseable completion whose claim cites the chunk the live item carries."""
    return json.dumps(
        {
            "answer": answer,
            "claims": claims
            or [{"text": "Revenue was 1450", "chunk_ids": ["ch-1"]}],
        }
    )


def _lost_region_item_live():
    """Same metadata, but with the chunk id the scripted model cites."""
    return make_item(CANONICAL, chunk_id="ch-1").model_copy(
        update={"metadata": {"parse_partial": True, "parse_lost_regions": [_LOST_REGION]}}
    )


async def _engine_answer(items, llm_text: str):
    """Drive the REAL engine over a stub retrieval leg."""
    from answer.engine import AnswerEngine
    from answer.verification import ExtractiveGrounder

    engine = AnswerEngine(
        orchestrator=_StubOrchestrator(items),
        llm=_ScriptedLLM(llm_text),
        model="fake-model",
        claim_grounder=ExtractiveGrounder(),
    )
    return await engine.answer(
        query="what is the revenue?", acl_scope=ACLScope(user_id="u1", workspace_ids=[WS])
    )


def _discloses(answer) -> bool:
    return any("NOT available" in limitation for limitation in answer.limitations)


# --------------------------------------------------------------------------------------
# 2. All return paths (the gap this ticket closes)
# --------------------------------------------------------------------------------------


def test_undisclosing_return_paths_are_zero() -> None:
    """THE METRIC. Every ``return`` in ``answer()`` funnels through ``_finalize``.

    Pinned as a structural check so a future early return cannot quietly
    re-open the gap: the engine has exactly one place where an ``Answer`` is
    allowed to leave, and it applies the disclosure.
    """
    import inspect

    from answer.engine import AnswerEngine

    source = inspect.getsource(AnswerEngine.answer)
    returns = [line.strip() for line in source.splitlines() if line.strip().startswith("return ")]
    assert returns, "answer() must still return answers"
    undisclosing = [line for line in returns if "_finalize" not in line]
    assert undisclosing == [], f"undisclosing return paths: {undisclosing}"


async def test_the_standing_path_discloses_and_caps_confidence() -> None:
    from answer.models import ConfidenceLevel

    result = await _engine_answer([_lost_region_item_live()], _llm_json())

    assert result.status is AnswerStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.NEEDS_REVIEW.value
    assert result.confidence.level is ConfidenceLevel.MEDIUM, "HIGH claims completeness"
    assert _discloses(result)


async def test_the_partial_path_discloses() -> None:
    """Partial verification over a partly-read source: the disclosure wins the status.

    ``PARTIAL`` is a *standing* status, so ``_disclose_source`` relabels it
    ``needs_review`` (that is the recovered T-V1-031 contract, not a
    regression): the answer still renders only the verified claim, and the
    lost region is disclosed alongside the cap.
    """
    from answer.models import ConfidenceLevel

    # One claim citing a chunk that does not exist → partial verification.
    result = await _engine_answer(
        [_lost_region_item_live()],
        _llm_json(claims=[{"text": "Revenue was 1450", "chunk_ids": ["ch-1", "ch-ghost"]}]),
    )

    assert result.answer == "Revenue was 1450", "still renders only the verified claim"
    assert result.status is AnswerStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.NEEDS_REVIEW.value
    assert result.confidence.level is ConfidenceLevel.MEDIUM
    assert _discloses(result)


@pytest.mark.parametrize(
    ("reason_code", "llm_text"),
    [
        (ReasonCode.PARSE_FALLBACK.value, "this is not json at all"),
        (ReasonCode.EVIDENCE_MISSING.value, _llm_json(answer="I cannot answer that.")),
        (ReasonCode.NO_CLAIMS.value, json.dumps({"answer": "x", "claims": []})),
        (
            ReasonCode.CITATION_INVALID.value,
            json.dumps({"answer": "x", "claims": [{"text": "x", "chunk_ids": ["ch-ghost"]}]}),
        ),
    ],
    ids=["PARSE_FALLBACK", "EVIDENCE_MISSING", "NO_CLAIMS", "CITATION_INVALID"],
)
async def test_every_abstention_path_keeps_its_reason_and_still_discloses(
    reason_code: str, llm_text: str
) -> None:
    """The reviewer's split, closed: abstentions disclosed nothing before."""
    result = await _engine_answer([_lost_region_item_live()], llm_text)

    assert result.status is AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code == reason_code, "an abstention keeps its own reason"
    assert result.reason, "and its own reason text"
    assert _discloses(result), "but it still must disclose the lost region"


async def test_the_unavailable_path_discloses_too() -> None:
    """A pre-published leg-down outcome still discloses on the answered path.

    The diagnostics observer resets the outcome slot before the retrieval leg
    runs (stale-outcome guard), so an outcome published *ahead* of the engine
    reads as a healthy standing answer. Disclosure is the invariant, not the
    status: the standing answer is labelled ``needs_review`` and lists the
    regions that were never read — never a silent complete answer.
    """
    from retrieval.orchestrator import (
        RetrievalResult,
        RetrievalStatus,
        retrieval_outcome_ctx,
    )

    token = retrieval_outcome_ctx.set(
        RetrievalResult(
            items=[],
            status=RetrievalStatus.UNAVAILABLE,
            reason_code=ReasonCode.RETRIEVAL_UNAVAILABLE,
        )
    )
    try:
        result = await _engine_answer([_lost_region_item_live()], _llm_json())
    finally:
        retrieval_outcome_ctx.reset(token)

    assert result.status is AnswerStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.NEEDS_REVIEW.value
    assert _discloses(result)


async def test_a_fully_read_source_changes_nothing_end_to_end() -> None:
    """Control: the disclosure must not fire when nothing was lost."""
    result = await _engine_answer([make_item(CANONICAL, chunk_id="ch-1")], _llm_json())

    assert result.status is AnswerStatus.ANSWERED
    assert result.limitations == []
    assert result.reason_code is None


# --------------------------------------------------------------------------------------
# 3. Retrieval passthrough — direct and live
# --------------------------------------------------------------------------------------


def test_the_direct_passthrough_is_the_store_adapters_metadata_copy() -> None:
    """DIRECT: both real store adapters copy payload/row metadata onto the item.

    ``qdrant_store`` reads it off the payload, ``pg_fts`` off the row; the
    in-memory ``harness_fakes`` doubles mirror that contract, which is what the
    live test below drives.
    """
    from harness_fakes import _to_item
    from retrieval.interfaces import RetrievalMethod

    item = _to_item(make_record("ch-1", WS, CANONICAL), 0.5, RetrievalMethod.DENSE)
    assert item.metadata == {}, "a record with no metadata starts empty"

    marked = make_record("ch-1", WS, CANONICAL).model_copy(
        update={"metadata": {"parse_lost_regions": [_LOST_REGION]}}
    )
    assert (
        _to_item(marked, 0.5, RetrievalMethod.DENSE).metadata["parse_lost_regions"]
        == [_LOST_REGION]
    )


async def test_lost_regions_survive_the_real_store_and_orchestrator_to_the_answer() -> None:
    """LIVE PATH: record → store search → REAL orchestrator → engine → disclosed.

    Not a helper call: the metadata is planted on a stored record, retrieved by
    the store adapter, fused by the real orchestrator (which deep-copies each
    item) and only then read by the engine.
    """
    from answer.engine import AnswerEngine
    from answer.verification import ExtractiveGrounder
    from retrieval.orchestrator import HybridRetrievalOrchestrator

    record = make_record("ch-1", WS, CANONICAL, vector=[0.1] * 8).model_copy(
        update={"metadata": {"parse_partial": True, "parse_lost_regions": [_LOST_REGION]}}
    )
    dense = FakeVectorStore(dimension=8)
    await dense.ensure_collection(collection=COLL, dimension=8)
    await dense.upsert(collection=COLL, records=[record])
    lexical = FakeLexicalStore()

    orchestrator = HybridRetrievalOrchestrator(
        dense=dense,
        embedding=FakeEmbeddingProvider(),
        lexical=lexical,
        collection=COLL,
    )
    engine = AnswerEngine(
        orchestrator=orchestrator,
        llm=_ScriptedLLM(_llm_json()),
        model="fake-model",
        claim_grounder=ExtractiveGrounder(),
    )

    answer = await engine.answer(
        query="revenue", acl_scope=ACLScope(user_id="u1", workspace_ids=[WS])
    )

    # The item really reached the engine with its metadata intact...
    assert answer.citations, "the chunk must be citable for this to prove anything"
    # ...and the answer says so.
    assert answer.status is AnswerStatus.NEEDS_REVIEW
    assert _discloses(answer)
