"""G5 reproducibility tests: source inventory, manifest, partition validation.

Locks the honesty contract from ``evals/graph_dataset_manifest.py`` (authorized
task V2-G4-G5-PREP-REREVIEW-3):

- Source files are hashed deterministically (SHA-256) and inventoried by family.
- A manifest round-trips (write -> load) and pins exact bytes + family_of.
- **Partition validation** rejects family overlap (dev/holdout leakage) and
  reports unassigned families; a clean split passes.
- **Gold-reference validation** rejects dangling labels (a cited source not
  present in the manifest) -- this catches invented labels for absent documents.
- Inventory is honest: it never fabricates files or families.

Hermetic: synthetic fixtures under ``tmp_path``; no dataset access, no network,
no dependencies beyond the stdlib.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

# `graph_dataset_manifest` lives in evals/ (not on the pyproject `pythonpath`), so
# locate it here so the spec acceptance command runs green without PYTHONPATH=evals.
sys.path.append(str(Path(__file__).resolve().parents[2] / "evals"))

from graph_dataset_manifest import (
    DatasetManifest,
    DuplicateSourcePathError,
    Partition,
    SourceFamily,
    SourceFile,
    SourceInventory,
    build_inventory,
    sha256_file,
    validate_evaluated_universe,
    validate_gold_refs,
    validate_partitions,
)

pytestmark = pytest.mark.unit


def _write(root, rel: str, data: bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


# --------------------------------------------------------------------------------------
# Hashing + inventory
# --------------------------------------------------------------------------------------


def test_sha256_is_deterministic_and_content_addressed(tmp_path) -> None:
    a = tmp_path / "a.bin"
    a.write_bytes(b"hello world")
    h1 = sha256_file(a)
    h2 = sha256_file(a)
    assert h1 == h2
    assert h1 == hashlib.sha256(b"hello world").hexdigest()


def test_build_inventory_groups_families_and_hashes(tmp_path) -> None:
    _write(tmp_path, "pdf/AAPL/2019/report.pdf", b"aapl-2019")
    _write(tmp_path, "pdf/AAPL/2019/notes.txt", b"aapl-2019-notes")
    _write(tmp_path, "pdf/MSFT/2020/report.pdf", b"msft-2020")
    _write(tmp_path, "metadata.jsonl", b"{}")

    def family(rel: str) -> str:
        parts = rel.split("/")
        if rel.endswith("metadata.jsonl"):
            return "meta"
        if "pdf" in parts:
            i = parts.index("pdf")
            return f"{parts[i + 1]}:{parts[i + 2]}"
        return "other"

    inv = build_inventory(tmp_path, family_fn=family)
    assert set(inv.family_ids()) == {"AAPL:2019", "MSFT:2020", "meta"}
    assert len(inv.families["AAPL:2019"].files) == 2  # pdf + notes
    expected = len(b"aapl-2019") + len(b"aapl-2019-notes")
    assert inv.families["AAPL:2019"].total_bytes == expected


def test_manifest_round_trip(tmp_path) -> None:
    _write(tmp_path, "pdf/A/1/x.pdf", b"x")
    inv = build_inventory(tmp_path, family_fn=lambda rel: "A:1")
    manifest = inv.to_manifest()
    out = tmp_path / "manifest.json"
    manifest.write(out)
    loaded = DatasetManifest.load(out)
    assert loaded.sha256_by_path() == manifest.sha256_by_path()
    assert loaded.family_of == manifest.family_of
    assert loaded.to_json_dict()["n_files"] == manifest.to_json_dict()["n_files"]


# --------------------------------------------------------------------------------------
# Partition validation (no family overlap / leakage)
# --------------------------------------------------------------------------------------


def test_partitions_reject_family_overlap() -> None:
    dev = Partition(name="dev", family_ids={"A:1", "A:2"})
    holdout = Partition(name="holdout", family_ids={"A:2", "B:1"})  # A:2 leaks!
    report = validate_partitions([dev, holdout])
    assert report.ok is False
    assert "A:2" in report.overlapping_families
    assert any("overlap" in m for m in report.messages)


def test_partitions_clean_split_passes() -> None:
    dev = Partition(name="dev", family_ids={"A:1", "A:2"})
    holdout = Partition(name="holdout", family_ids={"B:1", "B:2"})
    report = validate_partitions([dev, holdout], known_families={"A:1", "A:2", "B:1", "B:2"})
    assert report.ok is True
    assert report.overlapping_families == []
    assert report.unassigned_families == []


def test_partitions_report_unassigned_families() -> None:
    dev = Partition(name="dev", family_ids={"A:1"})
    holdout = Partition(name="holdout", family_ids={"B:1"})
    report = validate_partitions([dev, holdout], known_families={"A:1", "B:1", "C:1"})
    assert report.ok is False
    assert report.unassigned_families == ["C:1"]


def test_partitions_allow_unassigned_when_requested() -> None:
    dev = Partition(name="dev", family_ids={"A:1"})
    report = validate_partitions([dev], known_families={"A:1", "C:1"}, allow_unassigned=True)
    assert report.ok is True


# --------------------------------------------------------------------------------------
# Gold-reference validation (no dangling / invented labels)
# --------------------------------------------------------------------------------------


def test_gold_refs_reject_dangling_labels() -> None:
    manifest = DatasetManifest(
        root="/x",
        files=[SourceFile(rel_path="pdf/A/1/r.pdf", size_bytes=1, sha256="deadbeef")],
        family_of={"pdf/A/1/r.pdf": "A:1"},
    )
    # One ref resolves; one cites a document that is NOT present (invented label).
    report = validate_gold_refs(["pdf/A/1/r.pdf", "pdf/GHOST/9/missing.pdf"], manifest)
    assert report.ok is False
    assert report.dangling_gold_refs == ["pdf/GHOST/9/missing.pdf"]


def test_gold_refs_all_present_passes() -> None:
    manifest = DatasetManifest(
        root="/x",
        files=[SourceFile(rel_path="pdf/A/1/r.pdf", size_bytes=1, sha256="deadbeef")],
        family_of={"pdf/A/1/r.pdf": "A:1"},
    )
    report = validate_gold_refs(["pdf/A/1/r.pdf", "A:1"], manifest)
    assert report.ok is True
    assert report.dangling_gold_refs == []


# --------------------------------------------------------------------------------------
# Cross-root collision + partition-hardening regressions (V2-G5-MANIFEST-REREVIEW-2)
# --------------------------------------------------------------------------------------


def test_two_root_merge_uses_common_root_unique_resolvable_paths(tmp_path) -> None:
    """P2 regression: two sub-roots with the same leaf path must NOT collide.

    FinQA and TAT-DQA both ship a ``test/metadata.jsonl``. When their
    inventories are merged under one common root, the manifest paths must be
    unique and each must resolve (hash + family) to its intended source file.
    """
    common = tmp_path / "rag-eval"
    finqa = common / "FinQA" / "test"
    tatdqa = common / "TAT-DQA" / "test"
    finqa.mkdir(parents=True)
    tatdqa.mkdir(parents=True)
    (finqa / "metadata.jsonl").write_bytes(b"finqa-meta")
    (tatdqa / "metadata.jsonl").write_bytes(b"tatdqa-meta")

    inv_fin = build_inventory(common / "FinQA", family_fn=lambda rel: "finqa:metadata",
                              common_root=common)
    inv_tat = build_inventory(common / "TAT-DQA", family_fn=lambda rel: "tatdqa:metadata",
                              common_root=common)
    merged = SourceInventory(
        root=str(common),
        families={**inv_fin.families, **inv_tat.families},
    )
    manifest = merged.to_manifest()

    paths = [f.rel_path for f in manifest.files]
    assert len(paths) == len(set(paths)), "merged manifest paths must be unique"
    # Both leaf files resolve from the common root to their own content.
    by_path = {f.rel_path: f for f in manifest.files}
    resolved = {p: (common / p).read_bytes().decode() for p in paths}
    assert resolved["FinQA/test/metadata.jsonl"] == "finqa-meta"
    assert resolved["TAT-DQA/test/metadata.jsonl"] == "tatdqa-meta"
    # Both family mappings survive (no silent overwrite).
    assert manifest.family_of["FinQA/test/metadata.jsonl"] == "finqa:metadata"
    assert manifest.family_of["TAT-DQA/test/metadata.jsonl"] == "tatdqa:metadata"
    assert "FinQA/test/metadata.jsonl" in by_path
    assert "TAT-DQA/test/metadata.jsonl" in by_path


def test_duplicate_manifest_paths_rejected(tmp_path) -> None:
    """P2 regression: duplicate relative paths raise, never silently overwrite."""
    inv = SourceInventory(
        root=str(tmp_path),
        families={
            "a": SourceFamily(family_id="a", files=[SourceFile("test/metadata.jsonl", 1, "aa")]),
            "b": SourceFamily(family_id="b", files=[SourceFile("test/metadata.jsonl", 1, "bb")]),
        },
    )
    with pytest.raises(DuplicateSourcePathError) as exc:
        inv.to_manifest()
    assert "test/metadata.jsonl" in exc.value.duplicates


def test_partitions_reject_unknown_assigned_ids() -> None:
    """P2 regression: assigned family ids not in known_families are rejected."""
    dev = Partition(name="dev", family_ids={"real"})
    holdout = Partition(name="holdout", family_ids={"ghost"})  # unknown id
    report = validate_partitions([dev, holdout], known_families={"real"})
    assert report.ok is False
    assert "ghost" in report.unknown_assigned_families


def test_partitions_reject_duplicate_partition_names() -> None:
    """P2 regression: two partitions sharing a name are a malformed identity."""
    dev_a = Partition(name="dev", family_ids={"A:1"})
    dev_b = Partition(name="dev", family_ids={"B:1"})
    report = validate_partitions([dev_a, dev_b], known_families={"A:1", "B:1"})
    assert report.ok is False
    assert "dev" in report.duplicate_partition_names


def test_empty_holdout_is_inventory_only_not_evaluation_ready() -> None:
    """P2 regression: empty holdout -> ok structurally, but NOT evaluation-ready."""
    dev = Partition(name="dev", family_ids={"real"})
    holdout = Partition(name="holdout", family_ids=set())
    report = validate_partitions([dev, holdout], known_families={"real"})
    assert report.ok is True          # structurally valid: no overlap/unknown/unassigned
    assert report.evaluation_ready is False  # but empty holdout -> inventory-only
    assert any("inventory-only" in m for m in report.messages)


def test_nonempty_holdout_is_evaluation_ready() -> None:
    """A clean split with a non-empty holdout is evaluation-ready."""
    dev = Partition(name="dev", family_ids={"A:1"})
    holdout = Partition(name="holdout", family_ids={"B:1"})
    report = validate_partitions([dev, holdout], known_families={"A:1", "B:1"})
    assert report.ok is True
    assert report.evaluation_ready is True

# --------------------------------------------------------------------------------------
# Evaluated-family universe validation (V2-G5 re-review)
#
# Coverage is validated against the FROZEN evaluated-family universe (a selected
# eligible subset of the manifest), not the full manifest inventory. The universe
# must be non-empty and a subset of manifest family ids; partitions must then
# cover exactly that subset with no overlap, no duplicate names, and a non-empty
# holdout. No real labels/assignments/metrics: fixtures here are synthetic.
# --------------------------------------------------------------------------------------


def test_evaluated_universe_accepts_nonempty_subset() -> None:
    """A non-empty subset that is fully present in the manifest is ok + ready."""
    manifest_families = {"A:1", "A:2", "B:1", "B:2", "C:1"}
    evaluated = {"A:1", "B:1"}  # selected eligible subset, NOT the full manifest
    report = validate_evaluated_universe(evaluated, manifest_families)
    assert report.ok is True
    assert report.evaluation_ready is True
    assert report.empty_universe is False
    assert report.unknown == []


def test_evaluated_universe_rejects_unknown_id() -> None:
    """An id outside the manifest is an unknown universe member -> not ok."""
    report = validate_evaluated_universe({"A:1", "ghost"}, {"A:1", "B:1"})
    assert report.ok is False
    assert report.evaluation_ready is False
    assert report.unknown == ["ghost"]
    assert any("not in manifest" in m for m in report.messages)


def test_evaluated_universe_rejects_empty_universe() -> None:
    """An empty evaluated universe is rejected (cannot score against nothing)."""
    report = validate_evaluated_universe(set(), {"A:1", "B:1"})
    assert report.ok is False
    assert report.evaluation_ready is False
    assert report.empty_universe is True
    assert any("empty" in m for m in report.messages)


def test_known_subset_success_when_fully_partitioned() -> None:
    """Known non-empty universe + clean partition covering it + non-empty holdout."""
    manifest_families = {"A:1", "A:2", "B:1", "B:2"}
    evaluated = {"A:1", "B:1"}  # frozen eligible subset (A:2/B:2 NOT evaluated)
    u = validate_evaluated_universe(evaluated, manifest_families)
    assert u.ok and u.evaluation_ready  # universe gates the rest
    dev = Partition(name="dev", family_ids={"A:1"})
    holdout = Partition(name="holdout", family_ids={"B:1"})
    r = validate_partitions([dev, holdout], known_families=evaluated)
    assert r.ok is True
    assert r.evaluation_ready is True
    assert r.unassigned_families == []
    assert r.overlapping_families == []


def test_missing_eligible_assignment_fails() -> None:
    """A frozen family not assigned to any split is an unassigned gap -> not ok."""
    evaluated = {"A:1", "B:1"}  # B:1 eligible but unassigned
    dev = Partition(name="dev", family_ids={"A:1"})
    holdout = Partition(name="holdout", family_ids=set())
    r = validate_partitions([dev, holdout], known_families=evaluated)
    assert r.ok is False
    assert "B:1" in r.unassigned_families


def test_overlap_within_universe_fails() -> None:
    """A family in both dev and holdout (leakage) fails, even inside the subset."""
    evaluated = {"A:1", "B:1"}
    dev = Partition(name="dev", family_ids={"A:1", "B:1"})
    holdout = Partition(name="holdout", family_ids={"B:1"})
    r = validate_partitions([dev, holdout], known_families=evaluated)
    assert r.ok is False
    assert "B:1" in r.overlapping_families


def test_duplicate_split_names_fail() -> None:
    """Two partitions sharing a name are a malformed identity -> not ok."""
    evaluated = {"A:1", "B:1"}
    dev_a = Partition(name="dev", family_ids={"A:1"})
    dev_b = Partition(name="dev", family_ids={"B:1"})
    r = validate_partitions([dev_a, dev_b], known_families=evaluated)
    assert r.ok is False
    assert "dev" in r.duplicate_partition_names


def test_empty_holdout_is_inventory_only() -> None:
    """Clean coverage of the universe but empty holdout -> ok, not evaluation-ready."""
    evaluated = {"A:1"}
    dev = Partition(name="dev", family_ids={"A:1"})
    holdout = Partition(name="holdout", family_ids=set())
    r = validate_partitions([dev, holdout], known_families=evaluated)
    assert r.ok is True
    assert r.evaluation_ready is False
    assert any("inventory-only" in m for m in r.messages)
