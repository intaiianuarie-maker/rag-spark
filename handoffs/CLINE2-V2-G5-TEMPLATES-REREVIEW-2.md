# Handoff — V2 G5 gold/partition templates re-review 2 (docs-only correction, gated)

> Scope: `feat/v2-graph` (Stream B). Graph-only, **docs-only** (`docs/graph/G5-GOLD-AND-PARTITION-TEMPLATES.md`).
> **Prep only** — does not complete real G5 or V2; does NOT continue V4; does NOT
> edit shared `REUSE.MD`/`NOTICES.md`; does NOT touch Q9/G4 protected wiring
> (`packages/answer/*`, `packages/core/*`, API/UI); does NOT merge/deploy.
> No `COORDINATOR-NEXT-TASK-SPECS.md` file exists in this repo; § V2-G5 was
> taken from the task brief (portable `<G5_DATASETS_ROOT>` placeholder + frozen
> `evaluated_families` validation) and cross-checked against
> `reviews/V2-G5-TEMPLATES-REVIEW-1.md` (P2: universe scope + hard-coded path).
> Author: dev2 · 2026-10-11.

## What changed (docs-only)

`docs/graph/G5-GOLD-AND-PARTITION-TEMPLATES.md` — the code
(`evals/graph_dataset_manifest.py`: `validate_evaluated_universe`,
universe-scoped `validate_partitions`, `common_root` support), the test suite
(23 tests), and the JSON template (`<G5_DATASETS_ROOT>` + `evaluated_families`)
already carried the fix; only the `.md` was stale. Edits:

1. §"Files in this seam" row: partition template now described as frozen
   evaluated universe + dev/holdout assignment with portable placeholder.
2. §2 rewritten: `jsonc` block shows `"common_root": "<G5_DATASETS_ROOT>"`
   (contributor-supplied, never a machine-specific absolute path) and
   `"evaluated_families": []`; new "Why `evaluated_families`" paragraph
   (869-family inventory includes unparsed/unlicensed sources — coverage is
   against the frozen eligible subset, not the full manifest); rules list now
   covers `validate_evaluated_universe` (non-empty, all-known) + universe-scoped
   `validate_partitions` (unknown/overlap/dup-name rejection, non-empty holdout).
3. Command A rewritten as a two-step flow: (1) freeze/gate the universe via
   `validate_evaluated_universe`, (2) validate splits with `known_families` =
   the frozen list. Expected blank-form output corrected to observed behavior:
   universe `ok=False` ("universe is empty") + partition `ok=True` but
   `evaluation_ready=False` (empty holdout is inventory-only, never
   evaluation-ready).
4. §4 + verification note: `evaluated_families` empty, `common_root` portable,
   blank-form expectations match the command output.

No code, template-JSON, manifest, gold-row, or shared-file edits. No real
labels, splits, or results invented.

## Validation (all run, all green)

- `pytest tests/unit/test_graph_dataset_manifest.py -q` → **23 passed**.
- `ruff check evals/graph_dataset_manifest.py tests/unit/test_graph_dataset_manifest.py` → **All checks passed**.
- `mypy evals/graph_dataset_manifest.py` → **no issues**.
- Documented command A (empty template): universe `ok=False`/empty +
  partition `ok=True`/`evaluation_ready=False` (inventory-only). Hermetic
  filled-universe probe (2 manifest families → dev/holdout): universe `ok=True`,
  `evaluation_ready=True`.
- Documented command B (placeholder gold row): 1 row, 2 expected
  `family_id not in manifest` flags for the `<...>` illustrative tokens.
- Templates parse: partition JSON keys
  (`__comment__ common_root evaluated_families manifest notes splits`),
  `common_root == "<G5_DATASETS_ROOT>"`; gold NDJSON = 1 row / 10 keys;
  manifest pin unchanged (1248 files, 869 families, 282,355,978 bytes).
- `PYTHONPATH=evals pytest -q tests/unit/test_graph_factory.py tests/unit/test_graph_paired_eval.py tests/unit/test_graph_dataset_manifest.py` → **45 passed**.
  (Bare combined run without `PYTHONPATH=evals` trips a pre-existing
  `graph_paired_eval` circular import at collection — untouched by this
  docs-only task, out of scope.)

## Reviews addressed

- `V2-G5-TEMPLATES-REVIEW-1.md` P2 (partition scope broader than evaluated
  corpus): closed — universe freeze + subset-scoped coverage documented and
  wired in command A.
- `V2-G5-TEMPLATES-REVIEW-1.md` (hard-coded `common_root`): closed — portable
  `<G5_DATASETS_ROOT>` placeholder in template + doc; no machine-specific path
  remains in the `.md` (verified `NO-STALE-PATH`).

## External inputs / gates (unchanged)

Real G5 still needs: authored gold labels, a frozen real `evaluated_families` +
dev/holdout split, license decision (FinanceBench), Q9 + coordinator dispatch
per `G5-REAL-RUNNER-CONTRACT.md`, G4 wiring per `G4-INTEGRATION-CONTRACT.md`.
No merge/deploy authorized.
