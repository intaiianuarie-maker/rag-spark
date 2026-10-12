# Handoff — V2 G5 gold/partition templates (prep, gated)

> Scope: `feat/v2-graph` (Stream B). Graph-only, docs/templates only.
> **Prep only** — does not complete real G5 or V2; does NOT continue V4; does NOT
> edit shared `REUSE_MD`/`NOTICES.md`; does NOT touch Q9/G4 protected wiring
> (`packages/answer/*`, `packages/core/*`, API/UI); does NOT merge/deploy.
> Author: dev2 · 2026-10-11.

## What was added

Docs under `docs/graph/`:

| File | Role |
|---|---|
| `G5-GOLD-AND-PARTITION-TEMPLATES.md` | contributor-facing spec for the two G5 inputs the real run needs, plus validated build/validator commands wired to the committed manifest helpers. |
| `G5-REAL-GOLD-TEMPLATE.jsonl` | **gold-label template** — strict NDJSON, exactly **one** `synthetic-illustrative` row with `<...>` placeholder tokens (no real labels invented). Fields: `qid`, `question`, `language`, `slices`, `family_ids`, `evidence[]` (`order`, `family_id`, `chunk_id`, `document_id`, `page`, `section`, `span.start/end`), `answer`, `answer_type`, `label_source`, `notes`. |
| `G5-FAMILY-PARTITION-TEMPLATE.json` | **family dev/holdout assignment** — `splits.dev` / `splits.holdout` as lists of manifest `family_id`. **Intentionally empty** (blank form). |

The gold template is anchored to the real manifest fields (`family_of = {rel_path -> family_id}`, `files[]` with `sha256`/`size_bytes`) and to the G4 §2a citation-hydration contract (original-chunk text, ACL/version, exact `span`).

## What stays empty / illustrative (do NOT invent)

- `splits.dev` and `splits.holdout` are empty — no real family split assignments.
- The gold file has exactly one `synthetic-illustrative` row with placeholder ids — no real answers, no real chunks, no quality numbers.

## Validation (run against the committed manifest)

`docs/graph/G5-DATASET-MANIFEST.json` is the single source of `family_id` truth (files=1248, families=869, 282,355,978 bytes, unique resolvable paths, `family_of` covers the full file set).

- **Partition (command A in the doc):** `validate_partitions(splits, known_families=manifest.family_of.values())` via `evals/graph_dataset_manifest.py`.
  - Empty committed template → `ok=False`, `evaluation_ready=False` (families unassigned + empty holdout). **Expected blank-form behavior.**
  - Filled with non-overlapping known ids + non-empty holdout → `evaluation_ready=True`.
- **Gold (command B in the doc):** per-row checks that every `family_id`/`evidence[].family_id` is in the manifest, and that `chunk_id`/`document_id`/`span.start/end` are present.
  - Running B against the committed placeholder template → flags the `<...>` ids as "not in manifest" (expected; these are illustrative tokens, not real families).

Both commands were executed and produce the documented results (no crashes, correct flags).

## External inputs / gates (unchanged)

The templates are preparatory only. The real data path is still gated (see `G5-EVAL-REPORT.md`):
1. Authored gold labels (FinQA/TAT-DQA or license-cleared subset) — **not invented here**.
2. A real `dev`/`holdout` family partition by manifest `family_id` (this template's blank form).
3. License decision for any non-commercial corpus (FinanceBench).
4. Q9 + coordinator dispatch (REAL adapters) per `G5-REAL-RUNNER-CONTRACT.md`.
5. G4 protected wiring (citation hydration + answer-engine mode) per `G4-INTEGRATION-CONTRACT.md` §1–§2a — gated on accepted v1 Q9 + coordinator dispatch.

## Verification

- `ruff check evals/ tests/unit/test_graph_dataset_manifest.py` → **All checks passed.**
- `mypy packages/graph/` → **Success: no issues found in 10 source files.**
- `pytest tests/unit/test_graph_dataset_manifest.py tests/unit/test_graph_factory.py tests/unit/test_graph_paired_eval.py -q` → **37 passed.**
- Template syntax: `G5-REAL-GOLD-TEMPLATE.jsonl` parses line-by-line (1 row, 10 keys); `G5-FAMILY-PARTITION-TEMPLATE.json` parses; both validators return the documented blank-form output.
