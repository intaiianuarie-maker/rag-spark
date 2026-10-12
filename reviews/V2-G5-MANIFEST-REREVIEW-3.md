# V2 G5 dataset manifest — APPROVE scoped correction

The two P2 findings from `V2-G5-MANIFEST-REREVIEW-2.md` are closed in the
current additive manifest patch:

- `build_inventory(..., common_root=...)` records dataset paths relative to the
  shared root. The new two-root test verifies same-leaf files remain unique,
  resolvable, correctly hashed, and associated with distinct families.
- `SourceInventory.to_manifest()` rejects residual duplicate paths.
- `validate_partitions()` rejects unknown family IDs and duplicate partition
  names, and reports an empty or missing holdout as inventory-only through
  `evaluation_ready=False`.
- Dataset manifest, source inventory, and current handoff count now agree on
  1,248 files, 282,355,978 bytes, and 869 families. An independent rebuild to
  `/tmp` produced the same manifest SHA-256 as the checked-in JSON.

## Verification

- `PYTHONPATH=evals pytest -q tests/unit/test_graph_factory.py
  tests/unit/test_graph_paired_eval.py tests/unit/test_graph_dataset_manifest.py`
  — **37 passed**.
- `/root/ko-venv/bin/ruff check evals/graph_dataset_manifest.py
  evals/build_graph_manifest.py tests/unit/test_graph_dataset_manifest.py` —
  **All checks passed**.
- Direct probes reject unknown IDs and duplicate names and do not mark an empty
  holdout evaluation-ready.

This approves only the scoped G5 reproducibility-manifest correction. V2 is not
a complete product: real paired G5 quality evaluation still lacks authored gold
chunk labels and a valid frozen family split, while protected G4/Q9 integration
remains gated. No merge or deployment is approved.
