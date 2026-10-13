"""Unit tests for reindexing vectors and validation (RP-10 / QF-10).

Part 1 keeps the T-V1-014 boundary checks (no zero/NaN/Inf/wrong-dim vector is
ever built). Part 2 pins T-V1-045: the build-then-switch recovery calls
``QdrantVectorStore.promote_replacement`` — T-V1-015's verified switch — for
*every* switch it performs, prints that plan on a dry run with zero writes,
and keeps every refuse path. Everything runs against in-process fakes: no
services, no network (tests/README.md — unit tests must be green with compose
down).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from retrieval.adapters.infinity_embed import EmbeddingProviderError
from retrieval.adapters.qdrant_store import VectorStoreError

from scripts import reindex_all_chunks as reindex_mod
from scripts.reindex_all_chunks import ReindexRefused, reindex, validate_vector

#: Dimensions the fake provider reports and the fake index is built at.
_DIM = 4


async def _no_sleep(_seconds: float) -> None:
    """Retries back off instantly in tests; the wait is not what is under test."""
    return None


# --------------------------------------------------------------------------
# Part 1 — vector validation (T-V1-014): the zero path stays closed.
# --------------------------------------------------------------------------
def test_validate_vector_valid():
    vec = [0.1, -0.2, 0.5, 0.0]
    # 4 dimensions, has non-zero finite values
    validate_vector(vec, 4)


def test_validate_vector_rejects_dimension_mismatch():
    vec = [0.1, 0.2, 0.3]
    with pytest.raises(ValueError, match="Vector dimension mismatch"):
        validate_vector(vec, 4)


def test_validate_vector_rejects_all_zeros():
    vec = [0.0] * 1024
    with pytest.raises(ValueError, match="Manufactured all-zero vector rejected"):
        validate_vector(vec, 1024)


def test_validate_vector_rejects_nan():
    vec = [0.1, float("nan"), 0.3, 0.4]
    with pytest.raises(ValueError, match="non-finite"):
        validate_vector(vec, 4)


def test_validate_vector_rejects_inf():
    vec = [0.1, float("inf"), 0.3, 0.4]
    with pytest.raises(ValueError, match="non-finite"):
        validate_vector(vec, 4)


# --------------------------------------------------------------------------
# Fakes for part 2 (T-V1-045): the script under test talks to these only.
# --------------------------------------------------------------------------
def _rows(n: int) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            id=f"chunk-{i}",
            workspace_id="ws-1",
            document_id="doc-1",
            document_version_id="ver-1",
            text=f"text {i}",
            page=i,
            section=None,
            language="en",
            chunk_metadata=None,
        )
        for i in range(n)
    ]


def _inspection(**overrides: Any) -> dict[str, Any]:
    """Read-only inspection result as ``inspect_collection`` reports it."""
    info: dict[str, Any] = {
        "exists": False,
        "points": 0,
        "dims": None,
        "checked": 0,
        "models": [],
        "revisions": [],
        "recorded_dims": [],
        "error": None,
    }
    info.update(overrides)
    return info


def _unknown_model_inspection() -> dict[str, Any]:
    """An index whose points carry no identity — the T-V1-014 refuse case."""
    return _inspection(
        exists=True,
        points=3,
        dims=_DIM,
        checked=3,
        models=["<unrecorded>"],
        revisions=["<unrecorded>"],
        recorded_dims=["<unrecorded>"],
    )


class _FakeSession:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def execute(self, _query: Any) -> Any:
        rows = list(self._rows)
        return SimpleNamespace(all=lambda: rows)


class _ProviderBase:
    """Stands in for InfinityEmbeddingProvider: identity attrs, canned vectors."""

    model_name = "BAAI/bge-m3"
    revision = "BAAI/bge-m3"
    dimension = _DIM
    base_url = "http://infinity.test"

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.25] * _DIM for _ in texts]


class _VectorStoreBase:
    """Records every store call; the switch itself can be told to fail."""

    def __init__(self, harness: SimpleNamespace, *, collection: str) -> None:
        self.harness = harness
        self.collection = collection
        self.calls: list[tuple[Any, ...]] = []
        harness.stores.append(self)

    async def ensure_collection(self, *, collection: str, dimension: int, **_kw: Any) -> None:
        self.calls.append(("ensure_collection", collection, dimension, _kw.get("recreate", False)))

    async def upsert(self, *, collection: str, records: list[Any]) -> int:
        self.calls.append(("upsert", collection, list(records)))
        return len(records)

    async def promote_replacement(
        self, *, live: str, replacement: str, expected_points: int = 0
    ) -> None:
        self.calls.append(("promote_replacement", live, replacement, expected_points))
        error = self.harness.promote_error
        if error is not None:
            raise error

    async def close(self) -> None:
        self.calls.append(("close",))


class _LexicalBase:
    def __init__(self, harness: SimpleNamespace, **_kw: Any) -> None:
        self.harness = harness
        self.batches: list[list[Any]] = []
        harness.lexical.append(self)

    async def index(self, *, records: list[Any]) -> int:
        self.batches.append(list(records))
        return len(records)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point the reindex script at in-process fakes (no services, no network)."""
    state = SimpleNamespace(
        rows=_rows(3),
        inspection=_inspection(),
        inspected=[],
        stores=[],
        lexical=[],
        promote_error=None,
    )

    class FakeProvider(_ProviderBase):
        pass

    class FakeVectorStore(_VectorStoreBase):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(state, *args, **kwargs)

    class FakeLexicalStore(_LexicalBase):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(state, *args, **kwargs)

    async def fake_inspect(_base_url: str, collection: str) -> dict[str, Any]:
        state.inspected.append(collection)
        return dict(state.inspection)

    def fake_session_factory() -> Any:
        def factory() -> _FakeSession:
            return _FakeSession(state.rows)

        return factory

    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test")
    monkeypatch.setattr(reindex_mod, "InfinityEmbeddingProvider", FakeProvider)
    monkeypatch.setattr(reindex_mod, "QdrantVectorStore", FakeVectorStore)
    monkeypatch.setattr(reindex_mod, "PostgresLexicalStore", FakeLexicalStore)
    monkeypatch.setattr(reindex_mod, "inspect_collection", fake_inspect)
    monkeypatch.setattr(reindex_mod, "get_session_factory", fake_session_factory)
    return state


def _calls(store: _VectorStoreBase) -> list[tuple[Any, ...]]:
    return [c for c in store.calls if c[0] != "close"]


# --------------------------------------------------------------------------
# Part 2 — build-then-switch recovery through promote_replacement (T-V1-045).
# --------------------------------------------------------------------------
async def test_dry_run_prints_the_switch_plan_and_writes_nothing(
    harness: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="reindex")

    written = await reindex(
        dry_run=True, collection="knowledge", replacement="knowledge_v2", batch_size=2
    )

    assert written == 0
    # Zero writes: not even a store object is constructed on a dry run.
    assert harness.stores == []
    assert harness.lexical == []
    plan = caplog.text
    assert "switch plan" in plan
    assert (
        "promote_replacement(live='knowledge', replacement='knowledge_v2', expected_points=3)"
        in plan
    )
    assert "WRITES=0" in plan
    assert "aliases_changed=0" in plan


async def test_dry_run_refusal_reports_a_blocked_switch_plan(
    harness: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """A dry run must not offer a switch its real run would refuse to make."""
    caplog.set_level(logging.INFO, logger="reindex")
    harness.inspection = _unknown_model_inspection()

    written = await reindex(
        dry_run=True, collection="knowledge", replacement="knowledge_v2", batch_size=2
    )

    assert written == 0
    assert harness.stores == []
    plan = caplog.text
    assert "action=refuse" in plan
    assert "switch plan: BLOCKED" in plan
    assert "live='knowledge' keeps serving" in plan
    # No planned switch is printed when the run would refuse.
    assert "promote_replacement(live='knowledge', replacement='knowledge_v2'" not in plan


async def test_real_run_builds_into_the_replacement_then_promotes_it(
    harness: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="reindex")

    indexed = await reindex(collection="knowledge", replacement="knowledge_v2", batch_size=2)

    assert indexed == 3
    store = harness.stores[0]
    assert store.collection == "knowledge_v2"
    # Every write went to the replacement; the live collection is never a
    # write target, and nothing is destroyed to start the rebuild.
    assert [c[:2] for c in _calls(store) if c[0] == "upsert"] == [
        ("upsert", "knowledge_v2"),
        ("upsert", "knowledge_v2"),
    ]
    assert [c[:3] for c in _calls(store) if c[0] == "ensure_collection"] == [
        ("ensure_collection", "knowledge_v2", _DIM)
    ]
    assert all(c[3] is False for c in _calls(store) if c[0] == "ensure_collection")

    # Exactly one switch, handed to promote_replacement, after the last write.
    names = [c[0] for c in _calls(store)]
    assert names.count("promote_replacement") == 1
    last_upsert = max(i for i, name in enumerate(names) if name == "upsert")
    assert names.index("promote_replacement") > last_upsert
    promote = next(c for c in _calls(store) if c[0] == "promote_replacement")
    assert promote[1:] == ("knowledge", "knowledge_v2", 3)
    assert store.calls[-1] == ("close",)

    # No zero path reintroduced: every written vector is non-zero at the
    # provider's dimensions and carries the identity stamp.
    written = [r for c in _calls(store) if c[0] == "upsert" for r in c[2]]
    assert len(written) == 3
    assert all(len(r.vector) == _DIM and any(x != 0.0 for x in r.vector) for r in written)
    assert all(r.metadata["embedding_model"] == "BAAI/bge-m3" for r in written)
    # The FTS side reindexes the same validated records.
    assert sum(len(batch) for batch in harness.lexical[0].batches) == 3
    assert "switch complete: 'knowledge' now serves 'knowledge_v2'" in caplog.text


async def test_run_without_a_replacement_never_switches(
    harness: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="reindex")

    indexed = await reindex(collection="knowledge", batch_size=2)

    assert indexed == 3
    store = harness.stores[0]
    assert store.collection == "knowledge"
    assert [c[0] for c in _calls(store)] == [
        "ensure_collection",
        "upsert",
        "upsert",
    ]
    assert "switch plan" not in caplog.text


async def test_verified_replacement_is_kept_and_still_switched(
    harness: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-run after an interrupted switch: a verified replacement is kept."""
    caplog.set_level(logging.INFO, logger="reindex")
    harness.inspection = _inspection(
        exists=True,
        points=3,
        dims=_DIM,
        checked=3,
        models=["BAAI/bge-m3"],
        revisions=["BAAI/bge-m3"],
        recorded_dims=[str(_DIM)],
    )

    indexed = await reindex(collection="knowledge", replacement="knowledge_v2", batch_size=2)

    assert indexed == 3
    assert "plan: action=keep" in caplog.text
    store = harness.stores[0]
    # The already-verified replacement is neither refused nor rebuilt from
    # scratch — it is upserted into (idempotent) and promoted like any build.
    promote = next(c for c in _calls(store) if c[0] == "promote_replacement")
    assert promote[1:] == ("knowledge", "knowledge_v2", 3)


async def test_promote_failure_propagates_and_no_other_switch_runs(
    harness: SimpleNamespace,
) -> None:
    """The switch is only ever the store's verified one — and it can abort."""
    harness.promote_error = VectorStoreError(
        "replacement 'knowledge_v2' is incomplete: 2 of 3 expected points — nothing "
        "promoted; 'knowledge' is untouched"
    )

    with pytest.raises(VectorStoreError, match="nothing promoted"):
        await reindex(collection="knowledge", replacement="knowledge_v2", batch_size=2)

    store = harness.stores[0]
    names = [c[0] for c in _calls(store)]
    assert names.count("promote_replacement") == 1
    # No fallback switch of any kind: the script itself never deletes/renames.
    assert not any(name in {"delete_collection", "create_alias"} for name in names)
    assert all(c[3] is False for c in _calls(store) if c[0] == "ensure_collection")
    # The store is still closed on the failure path.
    assert store.calls[-1] == ("close",)


async def test_refused_index_still_raises_before_any_write(harness: SimpleNamespace) -> None:
    harness.inspection = _unknown_model_inspection()

    with pytest.raises(ReindexRefused, match="Never mix models"):
        await reindex(collection="knowledge")

    assert harness.stores == []
    assert harness.lexical == []


async def test_refused_replacement_still_raises_before_any_write(
    harness: SimpleNamespace,
) -> None:
    """--replacement does not become a way around the T-V1-014 guard."""
    harness.inspection = _unknown_model_inspection()

    with pytest.raises(ReindexRefused, match="Never mix models"):
        await reindex(collection="knowledge", replacement="knowledge_v2")

    # The replacement is the decision target, and nothing was written.
    assert harness.inspected == ["knowledge_v2"]
    assert harness.stores == []
    assert harness.lexical == []


async def test_replacement_must_differ_from_the_live_collection(
    harness: SimpleNamespace,
) -> None:
    with pytest.raises(ReindexRefused, match="different, non-empty name"):
        await reindex(collection="knowledge", replacement="knowledge")

    # Refused before the provider probe or any inspection.
    assert harness.inspected == []
    assert harness.stores == []


# --------------------------------------------------------------------------
# Part 3 — T-V1-060: one slow provider batch must not kill a 40-minute rebuild
# --------------------------------------------------------------------------
class _FlakyProvider:
    """Fails the first ``fail_times`` calls with the provider's own error type."""

    model_name = "BAAI/bge-m3"
    revision = "BAAI/bge-m3"
    dimension = _DIM
    base_url = "http://infinity.test"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise EmbeddingProviderError("infinity /embeddings request failed: ReadTimeout:")
        return [[0.25] * _DIM for _ in texts]


async def test_a_slow_batch_is_retried_and_the_run_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-V1-060: batch 11 of a 75-batch rebuild timed out at 120 s and aborted.

    The provider kept serving 200s immediately afterwards, so that batch was slow,
    not hung, and its texts were ordinary (median 30 chars) — so the response is a
    retry of *that* batch, never a split and never a bigger global ceiling.
    """
    delays: list[float] = []

    async def instant_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(reindex_mod.asyncio, "sleep", instant_sleep)
    provider = _FlakyProvider(fail_times=1)

    vectors = await reindex_mod.embed_batch_with_retry(provider, ["a", "b"])  # type: ignore[arg-type]

    assert vectors == [[0.25] * _DIM, [0.25] * _DIM]
    assert provider.calls == 2, "one failure, one retry — never a silent success"
    assert delays == [15.0], "one bounded backoff, not an unbounded spin"


async def test_a_persistently_slow_batch_fails_loudly_and_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausting the retries raises: a half-built replacement is never switched."""
    monkeypatch.setattr(reindex_mod.asyncio, "sleep", _no_sleep)
    provider = _FlakyProvider(fail_times=99)

    with pytest.raises(EmbeddingProviderError, match="ReadTimeout"):
        await reindex_mod.embed_batch_with_retry(provider, ["a"])  # type: ignore[arg-type]

    assert provider.calls == reindex_mod._EMBED_BATCH_ATTEMPTS


async def test_the_retry_budget_is_bounded_and_rides_out_a_provider_restart() -> None:
    """Bounded, and wide enough to outlast one model reload (~35 s on this box).

    T-V1-060 lost a rebuild to exactly that: the server restarted mid-run and the
    old 3-attempt / 5 s+10 s budget expired while it was still loading.
    """
    assert reindex_mod._EMBED_BATCH_ATTEMPTS == 4
    assert reindex_mod._EMBED_BATCH_BACKOFF_SECONDS == (15.0, 30.0, 45.0)
    assert sum(reindex_mod._EMBED_BATCH_BACKOFF_SECONDS) >= 35.0, (
        "the budget must cover a full model reload before the last attempt"
    )


async def test_a_failing_batch_prevents_the_switch(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a provider that never answers leaves the live name alone.

    This is the T-V1-060 failure mode at run level — the switch is unreachable
    once the retries are spent, so ``knowledge`` keeps serving the old index
    instead of being repointed at a half-built replacement.
    """
    monkeypatch.setattr(reindex_mod.asyncio, "sleep", _no_sleep)

    class DeadProvider(_ProviderBase):
        """Serves the dimension probe, then never answers a batch again."""

        calls = 0

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            type(self).calls += 1
            if type(self).calls == 1:  # the probe — must still succeed
                return [[0.25] * _DIM for _ in texts]
            raise EmbeddingProviderError("infinity /embeddings request failed: ReadTimeout:")

    monkeypatch.setattr(reindex_mod, "InfinityEmbeddingProvider", DeadProvider)

    with pytest.raises(EmbeddingProviderError):
        await reindex(collection="knowledge", replacement="knowledge_v2", batch_size=2)

    # probe + one exhausted batch of retries, and nothing else.
    assert DeadProvider.calls == 1 + reindex_mod._EMBED_BATCH_ATTEMPTS

    for store in harness.stores:
        assert all(call[0] != "promote_replacement" for call in store.calls), (
            "a failed rebuild must never reach the switch — knowledge keeps serving"
        )
