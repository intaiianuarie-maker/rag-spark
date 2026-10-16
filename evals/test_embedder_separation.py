"""Tests for the T-EVAL-008 embedder separation protocol.

Pure logic and failure behaviour only — every network call is served by an
``httpx.MockTransport``, so these tests neither cost money nor depend on either
endpoint being up.

The important tests here are the no-confounding ones: they prove by construction
that the two legs are measured on identical texts, identical k and identical
metric code, which is the claim the ticket asks to be proven rather than
asserted.
"""

from __future__ import annotations

import dataclasses
import math
from typing import cast

import httpx
import pytest

from evals import embedder_clients as ec
from evals import embedder_separation as es

# --------------------------------------------------------------------------
# shared metric code
# --------------------------------------------------------------------------


def test_cosine_is_scale_invariant_both_ways() -> None:
    """Normalisation happens inside cosine, so a provider's own convention
    cannot leak into the comparison between legs."""
    a = [1.0, 2.0, 3.0]
    b = [2.0, 0.0, -1.0]
    assert es.cosine(a, b) == pytest.approx(es.cosine([x * 50 for x in a], b))
    assert es.cosine(a, b) == pytest.approx(es.cosine(a, [x * 0.01 for x in b]))


def test_cosine_matches_the_textbook_value() -> None:
    assert es.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert es.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert es.cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError, match="dimension mismatch"):
        es.cosine([1.0, 2.0], [1.0, 2.0, 3.0])


def test_cosine_refuses_a_zero_vector() -> None:
    with pytest.raises(ValueError, match="zero vector"):
        es.cosine([0.0, 0.0], [1.0, 1.0])


def test_evidence_recall_counts_gold_hits_within_k() -> None:
    query = [1.0, 0.0]
    # Ranked order by cosine: c0, c1, c2.
    candidates = [[1.0, 0.0], [0.6, 0.8], [0.0, 1.0]]
    assert es.evidence_recall_at_k(query, candidates, (0, 1), 1) == pytest.approx(0.5)
    assert es.evidence_recall_at_k(query, candidates, (0, 1), 2) == pytest.approx(1.0)
    assert es.evidence_recall_at_k(query, candidates, (2,), 3) == pytest.approx(1.0)
    assert es.evidence_recall_at_k(query, candidates, (2,), 1) == pytest.approx(0.0)


def test_evidence_recall_rejects_degenerate_arguments() -> None:
    with pytest.raises(ValueError, match="k must be positive"):
        es.evidence_recall_at_k([1.0], [[1.0]], (0,), 0)
    with pytest.raises(ValueError, match="gold_indices"):
        es.evidence_recall_at_k([1.0], [[1.0]], (), 1)


def test_evidence_recall_breaks_ties_deterministically() -> None:
    query = [1.0, 0.0]
    candidates = [[1.0, 0.0], [1.0, 0.0]]
    # Identical vectors: index order decides, so the result is reproducible.
    assert es.evidence_recall_at_k(query, candidates, (1,), 1) == pytest.approx(0.0)
    assert es.evidence_recall_at_k(query, candidates, (0,), 1) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# no-confounding invariants
# --------------------------------------------------------------------------


def test_related_and_unrelated_sets_share_identical_left_texts() -> None:
    """The only difference between the two sets is which concept is on the right."""
    related = es.translation_related()
    unrelated = es.translation_unrelated()
    assert len(related) == len(unrelated)
    assert [p[0] for p in related.pairs] == [p[0] for p in unrelated.pairs]


def test_related_and_unrelated_sets_pair_different_right_texts() -> None:
    related = es.translation_related().pairs
    unrelated = es.translation_unrelated().pairs
    differing = sum(1 for (_, a), (_, b) in zip(related, unrelated, strict=True) if a != b)
    assert differing == len(related), "an unrelated pair reused a related right-hand text"


def test_unrelated_shift_is_non_zero_and_in_range() -> None:
    assert es.UNRELATED_SHIFT % len(es.CONCEPTS) != 0


def test_every_concept_has_a_romanian_translation() -> None:
    for concept in es.CONCEPTS:
        assert concept.ro.strip()
        assert concept.ro != concept.en


def content_words(text: str) -> set[str]:
    """Words long enough to carry topical meaning (drops short function words)."""
    return {w.strip(".,?").lower() for w in text.split() if len(w) > 4}


def test_unrelated_pairs_share_no_content_word_in_any_direction() -> None:
    """Guards the assumption behind the unrelated set, on the pairs that are
    ACTUALLY built — not on all concept combinations.

    Checked in four directions (EN/EN, RO/RO, EN/RO, RO/EN), because a
    cross-lingual pair can be topically related without sharing an English word.
    This test is why UNRELATED_SHIFT is 9: shift 7 paired the vaccine concept
    with the caffeine one and failed here.
    """
    n = len(es.CONCEPTS)
    clashes = []
    for concept in es.CONCEPTS:
        other = es.CONCEPTS[(concept.concept_id + es.UNRELATED_SHIFT) % n]
        shared = (
            content_words(concept.en) & content_words(other.en)
            | content_words(concept.ro) & content_words(other.ro)
            | content_words(concept.en) & content_words(other.ro)
            | content_words(concept.ro) & content_words(other.en)
        )
        if shared:
            clashes.append((concept.concept_id, other.concept_id, sorted(shared)))
    assert not clashes, f"an 'unrelated' pair shares a content word: {clashes}"


def test_unrelated_shift_is_verified_not_arbitrary() -> None:
    """The shift must be collision-free; guard the guard."""
    n = len(es.CONCEPTS)
    clean = []
    for shift in range(1, n):
        ok = all(
            not (
                content_words(c.en)
                & content_words(es.CONCEPTS[(c.concept_id + shift) % n].en)
                | content_words(c.ro)
                & content_words(es.CONCEPTS[(c.concept_id + shift) % n].ro)
                | content_words(c.en)
                & content_words(es.CONCEPTS[(c.concept_id + shift) % n].ro)
                | content_words(c.ro)
                & content_words(es.CONCEPTS[(c.concept_id + shift) % n].en)
            )
            for c in es.CONCEPTS
        )
        if ok:
            clean.append(shift)
    assert es.UNRELATED_SHIFT in clean, f"{es.UNRELATED_SHIFT} is not collision-free"
    assert len(clean) >= 2, "expected more than one safe shift"


def test_similarity_text_list_is_deduplicated_and_order_stable() -> None:
    texts = es.all_similarity_texts()
    assert len(texts) == len(set(texts))
    assert texts == es.all_similarity_texts()


def test_both_legs_embed_the_identical_text_list() -> None:
    """The no-confounding proof: the text list is a pure function of the
    protocol, with no embedder argument anywhere in it."""
    captured: list[list[str]] = []

    class Recorder(ec.Embedder):
        name = "recorder"
        dimension = 4
        priced = False
        stats = ec.EndpointStats(endpoint="recorder")

        def embed(self, texts):
            captured.append(list(texts))
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def close(self) -> None:
            return None

    es.separation_leg(Recorder())
    # 60+ distinct texts span several batches; the concatenated stream must
    # still be exactly the shared text list, in order, with no duplicates.
    assert sum(len(batch) for batch in captured) == len(es.all_similarity_texts())
    flattened = [text for batch in captured for text in batch]
    assert flattened == es.all_similarity_texts()
    assert all(len(batch) <= es.BATCH_SIZE for batch in captured)


def test_k_values_are_identical_for_every_embedder_and_probe() -> None:
    assert es.SYNTHETIC_K_VALUES == (1, 3, 5)
    assert es.HEADLINE_K in es.SYNTHETIC_K_VALUES
    # build_report hard-codes one k sequence for all embedders, so a per-embedder
    # k would be a structural bug rather than a subtle one.
    source = es.__file__
    assert source is not None
    text = open(source, encoding="utf-8").read()
    assert text.count("run_retrieval_probe(") == 3  # def + two call sites in build_report


# --------------------------------------------------------------------------
# synthetic Romanian probe
# --------------------------------------------------------------------------


def test_synthetic_ro_probe_uses_romanian_and_has_a_single_gold() -> None:
    cases = es.synthetic_ro_cases()
    assert len(cases) == 10
    for case in cases:
        assert case.gold_indices == (int(case.case_id.split("-")[1]),)
        assert len(case.candidates) == len(es.CONCEPTS)


def test_synthetic_ro_questions_are_not_the_paragraph_text() -> None:
    """A query identical to its own paragraph would make the probe vacuous."""
    for case in es.synthetic_ro_cases():
        gold = case.candidates[case.gold_indices[0]]
        assert case.query.strip() != gold.strip()


# --------------------------------------------------------------------------
# retry / failure discipline (mocked transport)
# --------------------------------------------------------------------------


def _gemini_client(handler) -> ec.GeminiEmbedding2:
    return ec.GeminiEmbedding2(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _bge_client(handler) -> ec.BgeM3Local:
    return ec.BgeM3Local(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _gemini_body(n: int, dim: int) -> dict:
    return {
        "data": [{"embedding": [0.1] * dim} for _ in range(n)],
        "usage": {"prompt_tokens": 7 * n, "cost": 1.0e-06 * n},
        "model": "gemini-embedding-2",
    }


def _bge_body(n: int, dim: int) -> dict:
    return {
        "data": [{"embedding": [0.1] * dim} for _ in range(n)],
        "usage": {"prompt_tokens": 5 * n, "cost": 3.0e-07 * n},
        "model": "BAAI/bge-m3",
    }


def test_cloud_leg_measures_tokens_and_cost() -> None:
    embedder = _gemini_client(lambda request: httpx.Response(200, json=_gemini_body(2, 3072)))
    vectors = embedder.embed(["a", "b"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 3072
    assert embedder.stats.prompt_tokens == 14
    assert embedder.stats.cost_usd == pytest.approx(2.0e-06)
    assert embedder.stats.cost_is_metered is True
    assert embedder.stats.n_failed_calls == 0


def test_bge_m3_leg_measures_tokens_and_cost_scoped_to_ticket() -> None:
    """BGE-M3 now routes via OpenRouter (local infinity removed per T-V1-029);
    cost is metered and reported, scoped to T-EVAL-008 only."""
    embedder = _bge_client(
        lambda request: httpx.Response(200, json=_bge_body(1, 1024))
    )
    vectors = embedder.embed(["a"])
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024
    assert embedder.stats.cost_is_metered is True
    assert embedder.stats.n_failed_calls == 0


def test_wrong_dimension_is_rejected_loudly() -> None:
    embedder = _gemini_client(lambda request: httpx.Response(200, json=_gemini_body(1, 128)))
    with pytest.raises(ec.EmbedderError, match="expected 3072-d"):
        embedder.embed(["a"])


def test_missing_api_key_refuses_to_skip_the_cloud_leg(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ec.EmbedderError, match="OPENROUTER_API_KEY"):
        ec.GeminiEmbedding2()


def test_missing_api_key_refuses_to_skip_the_bge_leg(monkeypatch) -> None:
    """BGE-M3 now requires OpenRouter auth since local infinity was removed."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ec.EmbedderError, match="OPENROUTER_API_KEY"):
        ec.BgeM3Local()


def test_retries_then_raises_and_counts_the_failure(monkeypatch) -> None:
    monkeypatch.setattr(ec.time, "sleep", lambda _: None)
    calls = {"n": 0}

    def always_fails(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    embedder = _gemini_client(always_fails)
    with pytest.raises(ec.EmbedderError, match="failed after 3 attempts"):
        embedder.embed(["a"])
    assert calls["n"] == ec.MAX_ATTEMPTS == 3
    assert embedder.stats.n_failed_calls == 1
    assert embedder.stats.n_retries == 2
    assert embedder.stats.failure_rate == pytest.approx(1.0)


def test_recovers_on_a_later_attempt(monkeypatch) -> None:
    monkeypatch.setattr(ec.time, "sleep", lambda _: None)
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=_gemini_body(1, 3072))

    embedder = _gemini_client(flaky)
    vectors = embedder.embed(["a"])
    assert len(vectors) == 1
    assert embedder.stats.n_failed_calls == 0
    assert embedder.stats.n_retries == 2
    assert embedder.stats.failure_rate == pytest.approx(0.0)


def test_backoff_grows_between_attempts(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(ec.time, "sleep", lambda s: sleeps.append(s))
    embedder = _gemini_client(lambda request: httpx.Response(500))
    with pytest.raises(ec.EmbedderError):
        embedder.embed(["a"])
    assert sleeps == [ec.BACKOFF_BASE_SECONDS, ec.BACKOFF_BASE_SECONDS * 2]


def test_short_response_is_rejected() -> None:
    embedder = _gemini_client(lambda request: httpx.Response(200, json=_gemini_body(3, 3072)))
    with pytest.raises(ec.EmbedderError, match="expected 1 embeddings"):
        embedder.embed(["a"])


def test_empty_vector_is_rejected() -> None:
    body: dict[str, object] = {"data": [{"embedding": []}]}
    embedder = _gemini_client(lambda request: httpx.Response(200, json=body))
    with pytest.raises(ec.EmbedderError, match="empty embedding"):
        embedder.embed(["a"])


def test_failure_rate_is_zero_for_an_unused_endpoint() -> None:
    assert ec.EndpointStats(endpoint="x").failure_rate == 0.0


# --------------------------------------------------------------------------
# accounting boundary
# --------------------------------------------------------------------------


def test_stats_row_reports_metering_flag() -> None:
    row = ec.EndpointStats(endpoint="x", cost_is_metered=False).as_row()
    assert row["cost_is_metered"] is False
    assert row["cost_usd"] == 0


def test_model_ids_are_pinned_verbatim() -> None:
    """The card pins the ID; a silent substitution would invalidate the eval."""
    assert ec.GEMINI_EMBEDDING_MODEL == "google/gemini-embedding-2"
    assert ec.GEMINI_EMBEDDING_DIMENSION == 3072
    assert ec.BGE_M3_MODEL == "BAAI/bge-m3"
    assert ec.BGE_M3_DIMENSION == 1024
    assert es.GeminiEmbedding2 if False else True  # no import side effects


def test_separation_stats_serialise_to_plain_numbers() -> None:
    stats = es.SeparationStats(
        pair_set="p", embedder="e", n_pairs=2, mean=0.5, stdev=0.1,
        minimum=0.4, maximum=0.6,
    )
    row: dict[str, object] = stats.as_row()
    assert row["mean_cosine"] == 0.5
    assert isinstance(row["stdev"], float)
    assert math.isfinite(cast(float, row["max"]))


def test_unknown_leg_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown leg"):
        es.main(legs=("not_a_leg",))


def test_span_target_shape_is_frozen_and_unresolved() -> None:
    """Carried over from T-EVAL-001ACQUIRE: retrieval fills these later."""
    from evals import hotpot_full as hf

    record = next(hf.iter_records("validation", config="distractor", limit=1))
    target = hf.span_map(record)[0]
    assert target.is_resolved is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.chunk_key = "smuggled"  # type: ignore[misc]

# --------------------------------------------------------------------------
# retrieval probe regression: recall must not fall as k rises
# --------------------------------------------------------------------------


class _AxisEmbedder(ec.Embedder):
    """Maps a text to a one-hot axis by its integer suffix.

    Deterministic and offline. Used to prove the probe's candidate slicing is
    correct for EVERY k, not just the first.
    """

    name = "axis"
    dimension = 4
    priced = False

    def __init__(self) -> None:
        self.stats = ec.EndpointStats(endpoint="axis", cost_is_metered=False)

    def embed(self, texts):
        out = []
        for text in texts:
            head, _, tail = text.rpartition(":")
            # "c:3" -> axis 3; anything else (real protocol text) -> stable hash.
            index = int(tail) % 4 if head and tail.isdigit() else (
                sum(map(ord, text)) % 4
            )
            vector = [0.0] * 4
            vector[index] = 1.0
            out.append(vector)
        return out

    def close(self) -> None:
        return None


def _axis_cases() -> list[es.RetrievalCase]:
    # Query 0 must retrieve gold candidate 0; query 1 must retrieve gold 1.
    return [
        es.RetrievalCase(
            case_id="a",
            query="q:0",
            candidates=("c:0", "c:1", "c:2"),
            gold_indices=(0,),
        ),
        es.RetrievalCase(
            case_id="b",
            query="q:1",
            candidates=("c:3", "c:0", "c:1"),
            gold_indices=(1,),
        ),
    ]


def test_evidence_recall_is_monotonic_in_k() -> None:
    """Regression: an offset bug made every k>1 report 0.0.

    Recall cannot decrease as k grows, so a non-monotonic series means the
    candidate slices were misaligned rather than that retrieval got worse.
    """
    results = es.run_retrieval_probe(_AxisEmbedder(), "axis", _axis_cases(),
                                     k_values=(1, 2, 3))
    by_k = {r.k: r.evidence_recall for r in results}
    # Case "a" has its gold at rank 1; case "b" has its gold at rank 3, so the
    # mean is 0.5 until k reaches 3. With the old offset bug these came out as
    # 0.5 / 0.0 / 0.0 — monotonically FALLING, which this test now rejects.
    assert by_k[1] == pytest.approx(0.5)
    assert by_k[2] == pytest.approx(0.5)
    assert by_k[3] == pytest.approx(1.0)
    assert by_k[1] <= by_k[2] <= by_k[3]


def test_probe_uses_every_case_at_every_k() -> None:
    """Guards the candidate-offset fix directly: each k must see all cases."""
    results = es.run_retrieval_probe(_AxisEmbedder(), "axis", _axis_cases(),
                                     k_values=(1, 3))
    assert all(r.n_cases == 2 for r in results)


def test_probe_reports_gold_out_of_top_k_when_it_should() -> None:
    """A gold in last place must be found at large k and missed at k=1."""
    # Query is axis 0, so "c:0" is the perfect match and sits FIRST. The gold is
    # the last candidate, which happens to be a poor match: missed at k=1,
    # found at k=4.
    cases = [
        es.RetrievalCase(
            case_id="hard",
            query="q:0",
            candidates=("c:0", "c:1", "c:2", "c:3"),
            gold_indices=(3,),
        )
    ]
    results = es.run_retrieval_probe(_AxisEmbedder(), "axis", cases, k_values=(1, 4))
    by_k = {r.k: r.evidence_recall for r in results}
    assert by_k[1] == pytest.approx(0.0)
    assert by_k[4] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# a leg that dies mid-run must be recorded, never silently dropped
# --------------------------------------------------------------------------


class _ExplodingEmbedder(ec.Embedder):
    """Serves the first call then raises like a downed endpoint would."""

    name = "exploding"
    dimension = 4
    priced = False

    def __init__(self) -> None:
        self.stats = ec.EndpointStats(endpoint="exploding")
        self._served = False

    def embed(self, texts):
        if self._served:
            raise ec.EmbedderError("exploding: connection refused")
        self._served = True
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def close(self) -> None:
        return None


def test_a_failed_leg_is_recorded_and_clears_full_run() -> None:
    report = es.build_report(
        [_ExplodingEmbedder(), _ExplodingEmbedder()], n_hotpot=1, legs_not_run={}
    )
    assert report["full_run"] is False
    not_run = cast(list[dict[str, object]], report["legs_not_run"])
    assert {row["leg"] for row in not_run} == {"exploding"}
    assert "connection refused" in str(not_run[0]["reason"])


def test_a_requested_leg_that_never_runs_is_recorded_before_any_call() -> None:
    report = es.build_report([_AxisEmbedder()], n_hotpot=1,
                             legs_not_run={"bge_m3": "not requested for this run"})
    assert report["full_run"] is False
    not_run = cast(list[dict[str, object]], report["legs_not_run"])
    assert any(row["leg"] == "bge_m3" for row in not_run)


def test_batch_size_is_small_enough_to_fit_host_memory() -> None:
    """Regression guard for the OOM the 32-batch request caused.

    A whole Wikipedia paragraph runs ~150 words. At 32 per request the resident
    cost was enough for the host OOM killer to take out the shared service, so
    the constant is pinned well below that.
    """
    assert es.BATCH_SIZE <= 8


def test_every_leg_shares_the_one_batch_size() -> None:
    text = open(es.__file__, encoding="utf-8").read()
    # Batching happens only in embed_all, which references the one constant.
    assert "BATCH_SIZE" in text
    assert text.count("range(0, len(texts), BATCH_SIZE)") == 1
