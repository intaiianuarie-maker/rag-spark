"""G5 reproducibility: source inventory + dataset manifest (V2 work package G5).

NOT product code. Standard-library only (no pyarrow/fastparquet — they are not
installed in the test venv and adding a dependency is out of scope). Parquet
files are **hashed and inventoried by bytes/size but not parsed**; their schemas
are reported from their dataset README/manifest where one exists, and are
otherwise marked UNKNOWN rather than guessed.

Purpose (per the coordinator's authorized task, V2-G4-G5-PREP-REREVIEW-3):

- Inventory **actual** available source material under a datasets root: per-file
  size + SHA-256, grouped by source family.
- Emit a **dataset manifest** (source hashes) that pins exactly what was present,
  so a paired eval is reproducible against a known corpus.
- Define and **validate** source-family dev/holdout partitions: no family may
  appear in both splits (no leakage), and every gold chunk/source reference must
  resolve to a present source (no dangling labels).
- Report **missing annotations honestly** (e.g. no gold chunk ids, no dev/holdout
  split) instead of inventing labels or calling synthetic/fake results real.

This module invents NOTHING. Where a label is absent it is recorded as missing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Read files in 1 MiB chunks so hashing a multi-GB parquet is memory-safe.
_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 of a file (hex). Memory-safe for large files."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class SourceFile:
    """One inventoried source file: relative path, byte size, SHA-256."""

    rel_path: str
    size_bytes: int
    sha256: str


@dataclass
class SourceFamily:
    """A group of source files that share a provenance family.

    The ``family_id`` is the leakage unit: the same family must never appear in
    both the dev and holdout partitions (a document and its questions, or two
    reports of the same company-year, are one family).
    """

    family_id: str
    files: list[SourceFile] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)


class DuplicateSourcePathError(ValueError):
    """Raised when two manifest entries claim the same relative path.

    A duplicate relative path means one entry would silently overwrite the
    other's ``family_of`` mapping and at least one would not resolve from the
    declared common root. Sources must be stored relative to a COMMON root so
    paths are unique and resolvable; this error is a guard against merging
    per-sub-root-relative inventories.
    """

    def __init__(self, duplicates: list[str]) -> None:
        self.duplicates = duplicates
        super().__init__(f"duplicate manifest paths (unresolvable from common root): {duplicates}")


@dataclass
class SourceInventory:
    """All source material under a root, grouped by family, with honest gaps."""

    root: str
    families: dict[str, SourceFamily]
    #: Human-readable notes on annotations that are present vs missing.
    notes: list[str] = field(default_factory=list)

    def all_files(self) -> list[SourceFile]:
        out: list[SourceFile] = []
        for fam in self.families.values():
            out.extend(fam.files)
        return sorted(out, key=lambda f: f.rel_path)

    def family_ids(self) -> set[str]:
        return set(self.families)

    def to_manifest(self) -> DatasetManifest:
        # Detect duplicate relative paths instead of silently overwriting the
        # family_of mapping (each path must resolve uniquely from the root).
        seen: set[str] = set()
        duplicates: list[str] = []
        family_of: dict[str, str] = {}
        for fid, fam in self.families.items():
            for f in fam.files:
                if f.rel_path in seen:
                    duplicates.append(f.rel_path)
                seen.add(f.rel_path)
                family_of[f.rel_path] = fid
        if duplicates:
            raise DuplicateSourcePathError(sorted(set(duplicates)))
        return DatasetManifest(
            root=self.root,
            files=self.all_files(),
            family_of=family_of,
            notes=list(self.notes),
        )


@dataclass
class DatasetManifest:
    """A reproducible pin of the source material a paired eval ran against."""

    root: str
    files: list[SourceFile]
    family_of: dict[str, str]
    notes: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "root": self.root,
            "n_files": len(self.files),
            "total_bytes": sum(f.size_bytes for f in self.files),
            "files": [
                {"rel_path": f.rel_path, "size_bytes": f.size_bytes, "sha256": f.sha256}
                for f in self.files
            ],
            "family_of": self.family_of,
            "notes": self.notes,
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json_dict(), indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> DatasetManifest:
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            root=d["root"],
            files=[SourceFile(**f) for f in d["files"]],
            family_of=dict(d.get("family_of", {})),
            notes=list(d.get("notes", [])),
        )

    def sha256_by_path(self) -> dict[str, str]:
        return {f.rel_path: f.sha256 for f in self.files}


# --------------------------------------------------------------------------------------
# Source-family dev/holdout partitions + gold-reference validation
# --------------------------------------------------------------------------------------


@dataclass
class Partition:
    """A named set of source families (e.g. ``dev`` or ``holdout``).

    Partitioning is by **family**, never by row: a family (a company-year report
    and its questions, or a document and its derived chunks) must live entirely
    in one split so no evidence leaks across the dev/holdout boundary.
    """

    name: str
    family_ids: set[str]


@dataclass
class ValidationReport:
    """Outcome of validating a set of partitions against a manifest.

    ``ok`` is True only when there is no family overlap, no unassigned family
    (unless allowed), no unknown assigned family, and no duplicate partition
    name. ``evaluation_ready`` is True only when ``ok`` **and** the ``holdout``
    partition is non-empty -- an empty or missing holdout is inventory-only,
    never evaluation-ready.
    """

    ok: bool
    evaluation_ready: bool = False
    overlapping_families: list[str] = field(default_factory=list)
    unassigned_families: list[str] = field(default_factory=list)
    unknown_assigned_families: list[str] = field(default_factory=list)
    duplicate_partition_names: list[str] = field(default_factory=list)
    dangling_gold_refs: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


#: Reserved partition name that marks the held-out evaluation split.
HOLDOUT_NAME = "holdout"


def validate_partitions(
    partitions: Sequence[Partition],
    *,
    known_families: Iterable[str] | None = None,
    allow_unassigned: bool = False,
) -> ValidationReport:
    """Validate dev/holdout partitions for family leakage and coverage.

    - **No family overlap:** a family in two partitions is leakage -> not ok.
    - **No duplicate partition names:** two partitions sharing a name are a
      malformed identity -> not ok.
    - **No unknown assigned ids:** when ``known_families`` is given, any assigned
      family id not in it is unknown -> not ok (catches assignments to families
      that do not exist).
    - **Coverage:** unless ``allow_unassigned``, every known family must be in
      exactly one partition; unassigned families are reported.
    - **Holdout readiness:** ``evaluation_ready`` is True only when ``ok`` and a
      ``holdout`` partition exists with at least one family. An empty or missing
      holdout is reported as inventory-only, never evaluation-ready.
    - ``known_families`` is the **evaluated family universe** to cover -- the
      frozen eligible subset (the non-empty, all-known result of
      `validate_evaluated_universe`), NOT the full manifest inventory.
      Coverage (unassigned families) is scoped to this subset only, so
      unrelated/unparsed/unlicensed manifest families are not forced into
      dev/holdout.
    """
    names = [p.name for p in partitions]
    dup_names = sorted({n for n in names if names.count(n) > 1})

    seen: dict[str, str] = {}
    overlapping: list[str] = []
    for part in partitions:
        for fid in part.family_ids:
            if fid in seen:
                overlapping.append(fid)
            seen[fid] = part.name

    unknown_assigned: list[str] = []
    unassigned: list[str] = []
    if known_families is not None:
        known = set(known_families)
        unknown_assigned = sorted(set(seen) - known)
        if not allow_unassigned:
            unassigned = sorted(known - set(seen))

    holdout_parts = [p for p in partitions if p.name == HOLDOUT_NAME]
    holdout_nonempty = any(p.family_ids for p in holdout_parts)

    ok = not overlapping and not unassigned and not unknown_assigned and not dup_names
    evaluation_ready = ok and holdout_nonempty

    messages: list[str] = []
    if dup_names:
        messages.append(f"duplicate partition names: {dup_names}")
    if overlapping:
        messages.append(
            f"family overlap across partitions (leakage): {sorted(set(overlapping))}"
        )
    if unknown_assigned:
        messages.append(f"assigned family ids unknown to the manifest: {unknown_assigned}")
    if unassigned:
        messages.append(f"families not assigned to any partition: {unassigned}")
    if not holdout_nonempty:
        messages.append(
            f"holdout partition '{HOLDOUT_NAME}' is empty or missing: "
            "inventory-only, not evaluation-ready"
        )
    if evaluation_ready:
        messages.append(
            f"evaluation-ready: {len(seen)} families across "
            f"{[p.name for p in partitions]}, non-empty holdout, no overlap/unknown"
        )
    return ValidationReport(
        ok=ok,
        evaluation_ready=evaluation_ready,
        overlapping_families=sorted(set(overlapping)),
        unassigned_families=unassigned,
        unknown_assigned_families=unknown_assigned,
        duplicate_partition_names=dup_names,
        dangling_gold_refs=[],
        messages=messages,
    )


@dataclass
class UniverseReport:
    """Outcome of checking a contributor-supplied evaluated family universe.

    ``ok`` is True only when the universe is **non-empty** and every id is present
    in the manifest (i.e. it is a valid, selected-eligible subset). A valid
    universe is a prerequisite for partition coverage (see
    :func:`validate_partitions` via ``known_families``).
    """

    ok: bool
    evaluation_ready: bool = False  # ok and non-empty
    empty_universe: bool = False
    unknown: list[str] = field(default_factory=list)  # ids in universe not in manifest
    messages: list[str] = field(default_factory=list)


def validate_evaluated_universe(
    evaluated_families: Iterable[str],
    manifest_families: Iterable[str],
) -> UniverseReport:
    """The evaluated family universe must be a **non-empty subset** of manifest families.

    Contributors supply ``evaluated_families`` -- the frozen, selected eligible
    gold/source families actually in the eval. This is a **subset** of the full
    manifest (the union of eligible gold/source families for the frozen dataset),
    **not** the entire manifest inventory. Validation rejects:

    - **Empty universe:** selecting nothing to evaluate is not allowed
      (``empty_universe=True``, ``ok=False``) -- you cannot score against nothing.
    - **Unknown ids:** any id not in ``manifest_families`` (the manifest's
      ``family_of`` values) is rejected here, so a partition can never assign a
      phantom family. This keeps the "unknown IDs" rejection at the universe
      level even when not every manifest family is part of the eval.

    Pass the resulting non-empty, all-known ``ok`` universe as partitions'
    ``known_families`` so dev/holdout **coverage** is checked against that
    subset only.
    """
    known = set(manifest_families)
    evaled = set(evaluated_families)
    unknown = sorted(evaled - known)
    empty_universe = not evaled
    ok = (not unknown) and (not empty_universe)
    evaluation_ready = ok
    if empty_universe:
        messages = [
            "evaluated family universe is empty; select a non-empty eligible subset "
            "before scoring"
        ]
    elif unknown:
        messages = [
            f"evaluated universe contains {len(unknown)} ids not in manifest: {unknown}"
        ]
    else:
        messages = [
            f"universe ok: {len(evaled)} evaluated families, all present in manifest"
        ]
    return UniverseReport(
        ok=ok,
        evaluation_ready=evaluation_ready,
        empty_universe=empty_universe,
        unknown=unknown,
        messages=messages,
    )


def validate_gold_refs(
    gold_refs: Iterable[str],
    manifest: DatasetManifest,
) -> ValidationReport:
    """Every gold chunk/source reference must resolve to a present source file.

    ``gold_refs`` are relative paths (or family ids) that the labelled examples
    cite as evidence. A reference with no matching file or family in the manifest
    is a **dangling label** -> not ok. This catches labels invented for documents
    that are not actually present.
    """
    present = set(manifest.family_of) | {f.rel_path for f in manifest.files}
    present |= set(manifest.family_of.values())  # family ids are valid refs too
    refs = list(gold_refs)
    dangling = sorted({ref for ref in refs if ref not in present})
    ok = not dangling
    messages = (
        [f"dangling gold references (no present source): {dangling}"]
        if dangling
        else [f"all {len(refs)} gold refs resolve to a present source"]
    )
    return ValidationReport(
        ok=ok,
        overlapping_families=[],
        unassigned_families=[],
        dangling_gold_refs=dangling,
        messages=messages,
    )


def build_inventory(
    root: Path,
    *,
    family_fn,
    include_suffixes: set[str] | None = None,
    common_root: Path | None = None,
) -> SourceInventory:
    """Walk ``root`` and group files into families via ``family_fn(rel_path)``.

    ``family_fn`` maps a POSIX relative path to a family id (the leakage unit).
    ``include_suffixes`` optionally restricts which file types are hashed
    (e.g. ``{"pdf", "jsonl"}``); when ``None`` every file is inventoried.
    Files are hashed with :func:`sha256_file`; parquet/binary files are hashed
    but never parsed.

    ``common_root`` is the root the returned paths are made **relative to**, for
    inventories that will be merged with siblings under one manifest root. When
    omitted, paths are relative to ``root``. Passing the shared parent ensures
    merged inventories produce unique, resolvable paths (e.g. ``FinQA/...`` and
    ``TAT-DQA/...`` instead of two colliding ``test/metadata.jsonl`` entries);
    :meth:`SourceInventory.to_manifest` additionally rejects any residual
    duplicate path rather than silently overwriting a family mapping.
    """
    root = Path(root)
    base = Path(common_root) if common_root is not None else root
    families: dict[str, SourceFamily] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if include_suffixes is not None and path.suffix.lstrip(".").lower() not in include_suffixes:
            continue
        fid = family_fn(rel)
        fam = families.setdefault(fid, SourceFamily(family_id=fid))
        fam.files.append(
            SourceFile(rel_path=rel, size_bytes=path.stat().st_size, sha256=sha256_file(path))
        )
    return SourceInventory(root=str(base), families=families)
