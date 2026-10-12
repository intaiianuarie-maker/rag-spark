# V2 G5 dataset manifest — REQUEST CHANGES

Reviewed `REPRODUCIBILITY-MANIFEST RESPONSE — 2026-10-11` against the checked-out
builder, validator, tests, and generated manifest.

## P2 — source paths collide across input roots

`build_inventory()` stores paths relative to each individual dataset root.
`build_graph_manifest.py` then merges FinQA and TAT-DQA family maps and writes
one manifest rooted at the common datasets directory. A hermetic two-root
reproduction with `FinQA/test/metadata.jsonl` and
`TAT-DQA/test/metadata.jsonl` produces two identical manifest paths,
`test/metadata.jsonl`; `family_of` silently overwrites one family mapping, and
neither entry resolves under the declared common root.

Make manifest paths relative to the common root or retain an explicit root per
source. Detect duplicate paths instead of silently overwriting mappings. Add a
temporary two-root regression that verifies both hashes resolve to the intended
source files and both family mappings survive.

## P2 — partition validation accepts unknown/malformed assignments

`validate_partitions([dev={"real"}, holdout={}], known_families={"real"})`
returns `ok=True`; assigned family IDs are never checked against the known set.
The validator also does not reject duplicate partition identities. Report an
empty holdout as inventory-only rather than evaluation-ready, and add negative
tests for unknown IDs and duplicate partition names.

## Verification

- `PYTHONPATH=evals pytest -q tests/unit/test_graph_dataset_manifest.py` —
  **9 passed**.
- Direct unknown-family reproduction still returns `ok=True`.
- Two-root collision reproduction yields duplicate relative paths and neither
  resolves from the manifest root.

The tests pass but do not cover the outstanding cases. G5 real paired evaluation
remains blocked on authored chunk labels and a valid family dev/holdout split;
this artifact does not accept G5 quality, protected G4 wiring, merge, or deploy.
