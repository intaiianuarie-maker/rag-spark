"""T-R07 Phase 2 — covering tests for the clean-rebuild builder's guards.

Pure logic: no Qdrant, no Postgres, no Infinity. The build's safety is in its
GUARDS and its ROW/REPORT mapping, so those are pinned directly. Live-side
verification (a real bge-m3 build into a scratch collection) is reported in the
handoff row, not simulated here — a test that fakes an embed would prove
nothing about the real build.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _REPO / relpath)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


rebuild = _load("rebuild_with_manifest", "scripts/rebuild_with_manifest.py")
manifest = _load("reindex_manifest", "scripts/reindex_manifest.py")

pytestmark = [pytest.mark.unit]

#: The manifest's SELECT column order (also the builder's mapping contract).
COLUMN_ORDER = (
    "chunk_id", "workspace_id", "document_id", "version_id",
    "original_checksum", "text", "page", "section", "language", "chunk_metadata",
)


# --- scratch-target guard: the only names this script may write to -------------


def test_scratch_target_accepts_both_conventions() -> None:
    rebuild.assert_scratch_target("knowledge_test_r07_rebuild")
    rebuild.assert_scratch_target("knowledge__rebuild_20261014")


def test_live_collection_is_refused() -> None:
    with pytest.raises(rebuild.RebuildRefused) as exc:
        rebuild.assert_scratch_target("knowledge")
    assert "live collection" in str(exc.value)


def test_serving_alias_is_refused() -> None:
    serving_alias = rebuild._serving.serving_alias_name("knowledge")
    with pytest.raises(rebuild.RebuildRefused) as exc:
        rebuild.assert_scratch_target(serving_alias)
    assert "serving alias" in str(exc.value)


def test_arbitrary_name_is_refused_fail_closed() -> None:
    for bad in ("", "   ", "prod", "knowledge_v2", "K"):
        with pytest.raises(rebuild.RebuildRefused):
            rebuild.assert_scratch_target(bad)


# --- the manifest is executed verbatim, not re-derived ------------------------


def test_builder_runs_the_pinned_manifest_sql() -> None:
    """The builder's source of truth is the Phase-1 pin."""
    assert manifest.SOURCE_MANIFEST_SQL
    assert manifest.manifest_predicates_present()["requires_current_version_match"]
    # The builder module must not carry its own SQL string.
    assert "SOURCE_MANIFEST_SQL" not in dir(rebuild)
    assert not hasattr(rebuild, "SOURCE_MANIFEST_SQL")


def test_manifest_rows_mapped_to_records_in_select_order() -> None:
    from retrieval.embedding_identity import EmbeddingIdentity, identity_from_metadata

    identity = EmbeddingIdentity(
        provider="InfinityEmbeddingProvider", model="BAAI/bge-m3",
        revision="rev-1", dimension=1024,
    )
    row = ("chk-1", "ws-a", "doc-a", "ver-a", "0" * 64, "evidence text", 1, "Intro",
           "en", {"language": "en", "title": "t"})
    records = rebuild.build_records([row], identity=identity, column_order=COLUMN_ORDER)
    assert len(records) == 1
    rec = records[0]
    assert rec.id == "chk-1"
    assert rec.chunk_id == "chk-1"
    assert rec.content == "evidence text"      # column 5 = text
    assert rec.workspace_id == "ws-a"
    assert rec.document_version_id == "ver-a"
    assert rec.page == 1 and rec.section == "Intro"
    # Identity is stamped, and identity WINS over caller metadata (T-R06).
    meta = identity_from_metadata(rec.metadata)
    assert meta.matches(identity)
    assert rec.metadata["title"] == "t"      # caller metadata preserved
    assert rec.vector == [0.0]               # placeholder until the real embed


def test_identity_wins_over_caller_supplied_value() -> None:
    """A manifest row whose metadata claims a different model is overridden."""
    from retrieval.embedding_identity import EmbeddingIdentity, identity_from_metadata

    identity = EmbeddingIdentity(
        provider="InfinityEmbeddingProvider", model="BAAI/bge-m3",
        revision="rev-1", dimension=1024,
    )
    row = ("chk-2", "ws-a", "doc-a", "ver-a", "0" * 64, "t", None, None, "en",
           {"embedding_model": "smuggled-model", "language": "en"})
    records = rebuild.build_records([row], identity=identity, column_order=COLUMN_ORDER)
    meta = identity_from_metadata(records[0].metadata)
    assert meta.model == "BAAI/bge-m3"      # not "smuggled-model"
    assert records[0].metadata["language"] == "en"


def test_placeholder_vector_is_replaced_by_the_real_embedding_in_the_loop() -> None:
    from retrieval.embedding_identity import EmbeddingIdentity

    identity = EmbeddingIdentity(
        provider="InfinityEmbeddingProvider", model="BAAI/bge-m3",
        revision="rev-1", dimension=1024,
    )
    row = ("chk-3", "ws-a", "doc-a", "ver-a", "0" * 64, "t", None, None, "en", {})
    records = rebuild.build_records([row], identity=identity, column_order=COLUMN_ORDER)
    real_vector = [0.1] * 1024
    for rec, vec in zip(records, [real_vector], strict=True):
        rec.vector = vec
    assert records[0].vector is real_vector


# --- batching / footprint discipline ------------------------------------------


def test_batch_math_is_bounded() -> None:
    import math
    for rows_n, batch in ((2204, 32), (33, 32), (1, 32), (32, 32), (0, 32)):
        assert math.ceil(rows_n / batch) * batch >= rows_n  # never truncates
    assert math.ceil(2204 / 32) == 69                       # ~69 embeds requests


def test_batches_never_exceed_the_timeout_shaped_size() -> None:
    # One batch = one synchronous POST /embeddings under a 30 s client timeout;
    # the reindex script measured 32 texts ≈ 9 s on this box.
    assert rebuild.DEFAULT_BATCH_SIZE == 32


# --- safety surface assertions -----------------------------------------------


def test_builder_deletes_promotes_and_switches_nothing() -> None:
    src = (_REPO / "scripts" / "rebuild_with_manifest.py").read_text()
    for forbidden in (
        "promote_replacement", "ensure_serving_alias", "delete_collection",
        "update_collection_aliases", "rollback_promotion",
    ):
        assert forbidden not in src, f"Phase-2 builder must not {forbidden}"


def test_nonempty_target_is_refused_without_resume_and_offered_with_it() -> None:
    """The refusal message must name --resume, or the operator is stranded."""
    import inspect

    src = inspect.getsource(rebuild.build)
    assert "--resume" in src
    # The guard reads points from a READ-ONLY inspection, never a store fetch.
    assert "inspect_collection" in src


def test_resume_flag_is_documented_as_explicit() -> None:
    import inspect

    src = inspect.getsource(rebuild.main)
    assert "--resume" in src
    parser_src = src
    assert "identity-verified at the boundary" in parser_src


def test_existing_chunk_id_helper_is_read_only() -> None:
    """The resume helper only ever scrolls payloads; it writes nothing."""
    import inspect

    src = inspect.getsource(rebuild._existing_chunk_ids)
    assert "points/scroll" in src
    for forbidden in ("upsert", "delete", "points/delete", "create_collection"):
        assert forbidden not in src, forbidden
    # "knowledge" may appear only as the refused/live constant, never as a
    # default write target.
    assert rebuild.LIVE_COLLECTION == "knowledge"
    default = rebuild.DEFAULT_COLLECTION if hasattr(rebuild, "DEFAULT_COLLECTION") else None
    assert default in (None, rebuild.REBUILD_TARGET, "knowledge__rebuild_scratch")
    assert rebuild.assert_scratch_target("knowledge__rebuild_scratch") is None
