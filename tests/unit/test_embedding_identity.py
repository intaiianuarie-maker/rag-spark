"""T-R06 — embedding identity on every write, verified before use.

The review proved an asymmetry: the rebuild path stamped provider / model /
revision / dimension onto every record, while NORMAL ingestion omitted them
entirely. So a collection could hold vectors from two models with nothing on the
record to tell them apart, and a wrong-model or wrong-dimension query provider
could be used against it without complaint.

The load-bearing test here is :func:`test_schema_matches_the_rebuild_script`,
which imports ``scripts/reindex_all_chunks.py`` and compares its
``provider_identity`` output to this module's. The parity is therefore proven
against the real rebuild code, not against a copy of it — and without editing
that file, which has another agent's uncommitted work in it.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

sys.path.insert(0, "packages")
sys.path.insert(0, "apps/api")

from retrieval.embedding_identity import (
    IDENTITY_KEYS,
    EmbeddingIdentity,
    EmbeddingIdentityError,
    audit_identity,
    identity_from_metadata,
    merge_identity,
    observed_dimension,
    provider_identity,
    verify_query_identity,
    verify_record_identity,
)

pytestmark = pytest.mark.unit


class _Provider:
    """Stand-in for OpenRouterProvider: same three identity attributes."""

    model_name = "google/gemini-embedding-2"
    revision = "google/gemini-embedding-2"
    dimension = 3072


class _OtherModel(_Provider):
    model_name = "BAAI/bge-m3"
    revision = "bge-m3@legacy"
    dimension = 1024


def _same_class_different_revision():
    """Two providers of the SAME class with different pinned revisions.

    Built as one class instantiated with a patched attribute on purpose: if the
    two objects had different class names, ``embedding_provider`` would mismatch
    first and the revision check would never be reached.
    """
    class _Rev:
        model_name = "google/gemini-embedding-2"
        revision = "google/gemini-embedding-2"
        dimension = 3072

    plain = _Rev()
    pinned = _Rev()
    pinned.revision = "google/gemini-embedding-2@sha256:deadbeef"
    return plain, pinned


class _LyingProvider(_Provider):
    """Declares 3072 but returns 1536 — the case observed_dimension exists for."""

    model_name = "google/gemini-embedding-2"
    dimension = 3072


def _identity(dim: int = 3072, model: str = "google/gemini-embedding-2") -> EmbeddingIdentity:
    return EmbeddingIdentity(
        provider="_Provider", model=model, revision="google/gemini-embedding-2", dimension=dim
    )


# --------------------------------------------------------------------------
# parity with the rebuild path
# --------------------------------------------------------------------------


def _rebuild_module():
    """Import the rebuild script WITHOUT editing it (foreign uncommitted work)."""
    path = pathlib.Path("scripts/reindex_all_chunks.py")
    spec = importlib.util.spec_from_file_location("t_r06_reindex", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_schema_matches_the_rebuild_script() -> None:
    """The two write paths must produce IDENTICAL metadata.

    Compared against the live rebuild code, not a copy of it, so the two cannot
    drift apart without this test failing.
    """
    rebuild = _rebuild_module()
    theirs = rebuild.provider_identity(_Provider(), 3072)
    ours = provider_identity(_Provider(), 3072).as_metadata()
    assert ours == theirs, "normal-ingestion identity has drifted from the rebuild path"
    assert tuple(ours.keys()) == IDENTITY_KEYS


def test_identity_keys_are_exactly_the_reviewed_set() -> None:
    assert IDENTITY_KEYS == (
        "embedding_provider",
        "embedding_model",
        "embedding_revision",
        "embedding_dimension",
    )


# --------------------------------------------------------------------------
# writes carry identity
# --------------------------------------------------------------------------


def test_identity_is_recorded_from_the_observed_dimension() -> None:
    identity = provider_identity(_LyingProvider(), 1536)
    assert identity.dimension == 1536, "declared width was trusted over the observed one"


def test_identity_refuses_a_provider_with_no_model() -> None:
    class Nameless(_Provider):
        model_name = ""

    with pytest.raises(EmbeddingIdentityError, match="model_name"):
        provider_identity(Nameless(), 3072)


@pytest.mark.parametrize("bad_dim", [0, -1])
def test_identity_refuses_a_nonsense_dimension(bad_dim: int) -> None:
    with pytest.raises(EmbeddingIdentityError, match="dimension"):
        provider_identity(_Provider(), bad_dim)


def test_observed_dimension_refuses_empty_and_ragged_batches() -> None:
    with pytest.raises(EmbeddingIdentityError, match="no vectors"):
        observed_dimension([])
    with pytest.raises(EmbeddingIdentityError, match="ragged"):
        observed_dimension([[0.1, 0.2], [0.1, 0.2, 0.3]])
    assert observed_dimension([[0.1, 0.2], [0.3, 0.4]]) == 2


# --------------------------------------------------------------------------
# METRIC 3 — identity independent of chunk metadata
# --------------------------------------------------------------------------


def test_caller_metadata_cannot_overwrite_trusted_identity() -> None:
    hostile = {
        "language": "en",
        "embedding_model": "attacker-model",
        "embedding_provider": "AttackerProvider",
        "embedding_revision": "rev-attacker",
        "embedding_dimension": 42,
    }
    merged = merge_identity(hostile, _identity())
    assert merged["embedding_model"] == "google/gemini-embedding-2"
    assert merged["embedding_provider"] == "_Provider"
    assert merged["embedding_revision"] == "google/gemini-embedding-2"
    assert merged["embedding_dimension"] == 3072
    # non-identity metadata survives untouched
    assert merged["language"] == "en"


def test_merge_does_not_mutate_the_callers_dict() -> None:
    caller = {"embedding_model": "attacker", "language": "ro"}
    merge_identity(caller, _identity())
    assert caller == {"embedding_model": "attacker", "language": "ro"}


# --------------------------------------------------------------------------
# METRIC 2 — incompatible provider refused, loudly, both directions
# --------------------------------------------------------------------------


def test_write_refuses_a_wrong_model() -> None:
    with pytest.raises(EmbeddingIdentityError, match="embedding_model"):
        verify_record_identity(_identity().as_metadata(), _identity(model="other/model"))


def test_write_refuses_a_wrong_dimension() -> None:
    with pytest.raises(EmbeddingIdentityError, match="embedding_dimension"):
        verify_record_identity(_identity().as_metadata(), _identity(dim=1024))


def test_write_refuses_a_wrong_revision() -> None:
    """Same provider class and model, different pinned revision — not comparable."""
    plain, pinned = _same_class_different_revision()
    requested = provider_identity(pinned, 3072)
    stored = provider_identity(plain, 3072).as_metadata()
    assert stored["embedding_provider"] == requested.as_metadata()["embedding_provider"]
    with pytest.raises(EmbeddingIdentityError, match="embedding_revision"):
        verify_record_identity(stored, requested)


def test_write_refuses_an_unlabelled_record() -> None:
    """A record with NO identity cannot be verified, so it is rejected.

    This is the T-R06 defect itself: the old normal-ingestion path wrote records
    with no identity at all, and this is what such a record now hits.
    """
    with pytest.raises(EmbeddingIdentityError, match="no embedding identity"):
        verify_record_identity({"language": "en"}, _identity())


def test_write_refuses_a_non_integer_dimension() -> None:
    metadata = dict(_identity().as_metadata())
    metadata["embedding_dimension"] = "not-a-number"
    with pytest.raises(EmbeddingIdentityError, match="not an integer"):
        verify_record_identity(metadata, _identity())


def test_query_refuses_a_wrong_model() -> None:
    with pytest.raises(EmbeddingIdentityError, match="embedding_model"):
        verify_query_identity(_identity(), _identity(model="other/model"))


def test_query_refuses_a_wrong_dimension() -> None:
    with pytest.raises(EmbeddingIdentityError, match="embedding_dimension"):
        verify_query_identity(_identity(), _identity(dim=1024))


def test_query_refuses_an_unknown_collection_identity() -> None:
    """Unknown is NOT assumed compatible — refusing is the whole point."""
    with pytest.raises(EmbeddingIdentityError, match="no recorded embedding identity"):
        verify_query_identity(None, _identity())


def test_a_matching_identity_passes_both_directions() -> None:
    metadata = _identity().as_metadata()
    assert verify_record_identity(metadata, _identity()) == _identity()
    assert verify_query_identity(_identity(), _identity()) == _identity()


def test_error_names_the_offending_field_not_just_a_hash() -> None:
    try:
        verify_record_identity(_identity(dim=3072).as_metadata(), _identity(dim=1536))
    except EmbeddingIdentityError as exc:
        assert "embedding_dimension" in str(exc)
        assert "3072" in str(exc) and "1536" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("a 3072 record must not verify against a 1536 provider")


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


def test_audit_reports_unlabelled_and_incompatible_separately() -> None:
    records = [
        _identity().as_metadata(),                       # good
        _identity().as_metadata(),                       # good
        {"language": "en"},                              # unlabelled
        _identity(dim=1536).as_metadata(),               # wrong dimension
        _identity(model="other").as_metadata(),          # wrong model
    ]
    report = audit_identity(records, _identity())
    assert report["total"] == 5
    assert report["labelled"] == 4
    assert report["unlabelled"] == 1
    assert report["incompatible"] == 2
    assert report["mismatch_reasons"] == {
        "embedding_dimension": 1,
        "embedding_model": 1,
    }


def test_audit_of_a_clean_collection_is_all_zero() -> None:
    records = [_identity().as_metadata() for _ in range(10)]
    report = audit_identity(records, _identity())
    assert report["unlabelled"] == 0
    assert report["incompatible"] == 0


def test_identity_roundtrips_through_metadata() -> None:
    original = provider_identity(_OtherModel(), 1024)
    assert identity_from_metadata(original.as_metadata()) == original


def test_two_providers_never_compare_equal_by_accident() -> None:
    gemini = provider_identity(_Provider(), 3072)
    bge = provider_identity(_OtherModel(), 1024)
    assert not gemini.matches(bge)
    # Provider names differ first (they are class names), so the reason names
    # provider — that is the first disagreeing field in canonical order.
    assert "embedding_provider" in gemini.mismatch_reason(bge)


def test_mismatch_reason_is_reported_in_canonical_field_order() -> None:
    """Deterministic and specific: the first disagreeing field, named."""
    mine = EmbeddingIdentity(provider="P", model="m", revision="r", dimension=8)
    theirs = EmbeddingIdentity(provider="P", model="m", revision="r2", dimension=9)
    assert mine.mismatch_reason(theirs) == (
        "embedding_revision: stored='r2' requested='r'"
    )
    assert mine.matches(
        EmbeddingIdentity(provider="P", model="m", revision="r", dimension=8)
    )


# --------------------------------------------------------------------------
# production source: normal ingestion must stamp, and must not be bypassable
# --------------------------------------------------------------------------


def test_normal_ingestion_stamps_identity_after_caller_metadata() -> None:
    """Source-level guard on the ingestion write path.

    Parsed rather than grepped so an explanatory comment cannot satisfy it: the
    ``merge_identity(`` call must actually WRAP the caller metadata, not sit
    beside it.
    """
    import ast

    source = pathlib.Path("apps/worker/jobs/ingest_pipeline.py").read_text(encoding="utf-8")
    assert "merge_identity(" in source
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "merge_identity"
    ]
    assert calls, "normal ingestion no longer calls merge_identity"
    for call in calls:
        assert len(call.args) == 2, "merge_identity needs metadata and identity"
        # The metadata argument must be a dict literal containing the caller's
        # own metadata spread, so identity is applied on top of it.
        assert isinstance(call.args[0], ast.Dict)


def test_normal_ingestion_derives_identity_from_observed_vectors() -> None:
    source = pathlib.Path("apps/worker/jobs/ingest_pipeline.py").read_text(encoding="utf-8")
    assert "provider_identity(" in source
    assert "observed_dimension(vectors)" in source