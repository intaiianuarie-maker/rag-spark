# T-V1-052 scratch proposal — answer-layer lost-region disclosure (moved tests, nothing deleted)

**Origin:** moved verbatim out of `tests/unit/test_format_matrix.py` by the
**T-V1-031b REPAIR** ticket, per reviewer verdict on T-V1-031
("REVERT `packages/answer/engine.py` and MOVE the engine-dependent disclosure
tests into a scratch proposal — do NOT delete the insight; T-V1-052 now exists
for the full all-paths work"). This file is that proposal/scratch: the insight
lives here until T-V1-052 re-homes it into the answer-layer test suite.

## What moved and why

- 4 tests + 2 helpers (below), formerly the "answer layer" section of the
  format-matrix file. They call `AnswerEngine._disclose_source`, which is
  **answer-layer surface** — out of T-V1-031's allowlist (ingestion only).
- `packages/answer/engine.py` was reverted to its pre-T-V1-031 state
  (`_disclose_source` / `_collect_lost_regions` / `_limitation_text` removed
  from the worktree). The full implementation as committed in T-V1-031's
  commit is **recoverable verbatim** at any time:
  `git show 7ce9711:packages/answer/engine.py` (the three helpers live at
  ~lines 329-385 of that revision).
- Matrix file went 37 → 33 tests (`37-minus-moved`), all green; the ingestion
  half of the contract stays pinned there
  (`test_parse_disclosure_rides_on_every_chunk_only_when_regions_were_lost`
  pins `PARSE_LOST_REGIONS_KEY == "parse_lost_regions"`).

## Known gaps the T-V1-052 card must close (from the reviewer split)

1. **3 `INSUFFICIENT_EVIDENCE` early returns bypass `_disclose_source`** in
   `packages/answer/engine.py` — an abstention over a partially-read source
   did not disclose. Wrap ALL return paths (standing + abstention).
2. **Retrieval metadata passthrough unchecked** — nothing proves
   `parse_lost_regions` survives store → retrieval onto `RetrievedItem.metadata`.
   The tests below exercise the helper directly; T-V1-052's success criteria
   demand direct **and** live-path tests, not helper-only.
3. Ticket card: "undisclosing return paths: 0"; confidence capped with a
   limitation; abstention keeps its own reason + still discloses.

## Moved tests (verbatim from `tests/unit/test_format_matrix.py` @ 7ce9711)

Needed imports at the receiving site:
`core.errors.{AnswerStatus, ReasonCode}`, `harness_fakes.make_item`.

```python
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


def _item_with_lost_regions():
    return make_item("Revenue was 1450 on page 1").model_copy(
        update={
            "metadata": {
                "parse_partial": True,
                "parse_lost_regions": [
                    {
                        "reason": "no_text_layer",
                        "lost_pages": [2],
                        "page_count": 2,
                        "readable_pages": 1,
                    }
                ],
            }
        }
    )


def test_an_answer_over_an_omitted_region_is_never_confident() -> None:
    from answer.engine import AnswerEngine
    from answer.models import ConfidenceLevel

    answered = AnswerEngine._disclose_source(_answer(), [_item_with_lost_regions()])
    assert answered.status is AnswerStatus.NEEDS_REVIEW
    assert answered.reason_code == ReasonCode.NEEDS_REVIEW.value
    assert answered.confidence.level is ConfidenceLevel.MEDIUM, "HIGH claims completeness"
    assert any("[2]" in limitation for limitation in answered.limitations)
    assert "NOT available" in answered.limitations[-1]


def test_an_answer_over_a_fully_read_source_is_untouched() -> None:
    from answer.engine import AnswerEngine
    from answer.models import ConfidenceLevel

    clean_item = make_item("Revenue was 1450 on page 1")
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
    result = AnswerEngine._disclose_source(abstained, [_item_with_lost_regions()])
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
```

## Note for the receiving ticket

The last test (`..._metadata_key_is_the_one_ingestion_writes`) has an
ingestion-side half (`PARSE_LOST_REGIONS_KEY == "parse_lost_regions"`) that is
**already pinned** by the matrix file's `test_parse_disclosure_rides_on_every_chunk_only_when_regions_were_lost`;
when re-homing, keep the engine-source-inspection half here so the two sides
stay cross-checked without duplicating the ingestion pin.

No product code, no commits, no deploy/restart — scratch proposal only.
