# G5 — contributor gold-label and family-partition templates (real G5 prep, no data committed)

> Docs-only, graph-only seam. **Empty/illustrative only** — does not invent real
> labels, split assignments, or results. No Q9 / G4 protected wiring, no V4 work,
> no shared `REUSE.MD`/`NOTICES.md` edits, no merge/deploy.
>
> Author: dev2 (Stream B) · 2026-10-10 · branch `feat/v2-graph`.

This documents the **exact** contributor-facing input formats the REAL G5 paired
run consumes, and how to validate them against existing manifest helpers — *before*
anyone authors a single real label. These files exist so the G5 data gate can be
prepared deterministically once the labelled corpus + license decision land
(see `docs/graph/G5-EVAL-REPORT.md` §"Honest dataset status — BLOCKER" and
`docs/graph/G5-REAL-RUNNER-CONTRACT.md`).

## Files in this seam

| File | Role |
|---|---|
| `G5-DATASET-MANIFEST.json` | existing pinned manifest: `root`, `files[]` (`rel_path`,`size_bytes`,`sha256`), `family_of: {rel_path -> family_id}`, `notes`. The single source of truth for **family_id** values. |
| `G5-REAL-GOLD-TEMPLATE.jsonl` | **gold-label template** — one JSON object per line (NDJSON). See field spec below. Contains only a single `synthetic-illustrative` row plus placeholders. |
| `G5-FAMILY-PARTITION-TEMPLATE.json` | **frozen evaluated universe + family dev/holdout assignment** — `evaluated_families` (frozen eligible subset of manifest `family_id`), `splits.dev`/`splits.holdout` as lists of those ids, `common_root` as portable `<G5_DATASETS_ROOT>` placeholder. **Intentionally empty** (blank form); contributors freeze the universe, fill the splits, and validate both. |
| `G5-GOLD-AND-PARTITION-TEMPLATES.md` | this doc. |

## 1. Gold-label template — `G5-REAL-GOLD-TEMPLATE.jsonl`

One JSON object **per line** (no comments, no blank lines). Required fields and
their exact meanings:

| Field | Type | Required | Meaning / constraint |
|---|---|---|---|
| `qid` | string | ✅ | Stable question id. Unique across the gold set and stable across regenerations (so slices/runs can be diffed). |
| `question` | string | ✅ | Question text. |
| `language` | string | ✅ | ISO 639-1 code (`"en"` …). |
| `slices` | string[] | ❌ | Slice tags for per-slice `EvidenceRecall@K` / grounded-success deltas (`"arithmetic","comparison","multi-hop"` etc.). Omit or `[]` if none. |
| `family_ids` | string[] | ✅ | **Every** manifest `family_id` whose evidence this gold answer cites. MUST be a subset of `G5-DATASET-MANIFEST.json` → `family_of` **values** (validate this). The union over the gold set's **holdout** families is what the partition must cover. Keep disjoint across dev/holdout at the family level. |
| `evidence[].order` | int | ✅ | 1-based position in the multi-hop chain (ordering is significant for multi-hop). |
| `evidence[].family_id` | string | ✅ | Manifest family_id of this evidence (must also appear in top-level `family_ids`). |
| `evidence[].chunk_id` | string | ✅ | **Our** chunk id — resolvable in the authoritative chunk store. |
| `evidence[].document_id` | string | ✅ | Authoritative document id (for hydration / ACL / version checks per G4 §2a). |
| `evidence[].page` | int | ❌ | Source page, if applicable. |
| `evidence[].section` | string | ❌ | Source section, if applicable. |
| `evidence[].span.{start,end}` | int | ✅ | Byte/char offsets **in the original chunk text** (not the synthesized `[Knowledge Graph Context]` passage) — required by G4 §2a "Exact source span". |
| `answer` | string | ✅ | Grounded answer. For multi-span answers, semicolon-joined spans. |
| `answer_type` | string | ✅ | One of `multi_hop`,`arithmetic`,`comparison`,`factoid`,`multi_span`. |
| `label_source` | string | ✅ | Provenance. Real data: `human-expert` \| `program-synthetic`. This template's single example row is `synthetic-illustrative` and **must NOT** be used as real eval data. |
| `notes` | string | ❌ | Free text. |

> **Citation-hydration contract (G4 §2a) is non-negotiable before grounding:** every
> `evidence[].chunk_id` must hydrate to the **original** chunk text from the
> authoritative store, must pass ACL/version/scope checks for `document_id`, and
> must have a locatable `span` in that original text. The synthesized
> `[Knowledge Graph Context]` passage is context only — never a citation source.

## 2. Frozen evaluated universe + family dev/holdout assignment — `G5-FAMILY-PARTITION-TEMPLATE.json`

```jsonc
{
  "manifest": "docs/graph/G5-DATASET-MANIFEST.json",
  "common_root": "<G5_DATASETS_ROOT>",   // contributor-supplied path; portable placeholder, NOT a machine-specific absolute path
  "evaluated_families": [],              // FROZEN eligible subset of manifest family_id values (NOT the full inventory)
  "splits": {
    "dev":     [],   // contributor fills with evaluated family_id values
    "holdout": []    // MUST be non-empty for evaluation_ready=True
  }
}
```

**Why `evaluated_families` (frozen universe):** the pinned manifest inventories
*every* available source (869 families), including datasets not parsed or
licensed for this evaluation. Partition coverage is therefore checked against
the frozen evaluated universe — the union of eligible gold/source families for
the frozen dataset — **not** the full manifest. Requiring all 869 inventory
families to be assigned would force irrelevant families into the split. Freeze
the universe first (see `validate_evaluated_universe`), then validate coverage
against that subset only.

**Rules enforced by `validate_evaluated_universe` + `validate_partitions` (see `evals/graph_dataset_manifest.py`):**
- `evaluated_families` must be a **non-empty subset** of the manifest's
  `family_of` values (empty → `empty_universe`, `ok=False`; unknown id →
  `unknown`, `ok=False`). A contributor can never score against nothing or a
  phantom family. Freeze this list before scoring; the frozen list is the
  `known_families` universe for the partition check.
- Every `family_id` in `splits.*` is checked against the **frozen universe**
  (otherwise → `unknown_assigned_families`, `ok=False`).
- A family appearing in **both** `dev` and `holdout` → `overlapping_families`
  (leakage), `ok=False`.
- `holdout` **must be non-empty**; an empty/missing holdout →
  `evaluation_ready=False` (inventory-only).
- Duplicate split names are rejected.
- `evaluation_ready` is `True` only when structurally clean **and** holdout is
  non-empty.

## 3. Validation commands (run against this committed manifest)

Use the on-disk manifest and the helpers in `evals/graph_dataset_manifest.py`.

**A. Freeze the evaluated universe, then validate the family partition:**

```bash
cd "/root/code/Super Rag/knowledge-os"
PYTHONPATH='evals:packages:apps/api:apps/worker:.:tests:tests/fixtures' \
  APP_ENV=test LOG_JSON=false \
  /root/ko-venv/bin/python3 - <<'PY'
import json
from graph_dataset_manifest import (
    validate_evaluated_universe,
    validate_partitions,
    Partition,
)

manifest = json.load(open('docs/graph/G5-DATASET-MANIFEST.json'))
manifest_families = set(manifest['family_of'].values())

tpl = json.load(open('docs/graph/G5-FAMILY-PARTITION-TEMPLATE.json'))
# Step 1 — freeze/gate the evaluated universe (non-empty subset of the manifest).
u = validate_evaluated_universe(tpl.get('evaluated_families', []), manifest_families)
print('universe_ok       =', u.ok)
print('universe_empty    =', u.empty_universe)
print('universe_unknown  =', u.unknown)
for m in u.messages:
    print('-', m)

# Step 2 — validate dev/holdout coverage against the FROZEN universe only
# (not the full manifest inventory).
part = tpl['splits']
partitions = [Partition(name=name, family_ids=set(ids)) for name, ids in part.items()]
r = validate_partitions(partitions, known_families=set(tpl.get('evaluated_families', [])))

print('ok               =', r.ok)
print('evaluation_ready =', r.evaluation_ready)
print('unassigned       =', r.unassigned_families)
print('overlapping      =', r.overlapping_families)
print('unknown_assigned =', r.unknown_assigned_families)
print('duplicate_names  =', r.duplicate_partition_names)
for m in r.messages:
    print('-', m)
PY
```

Expected for the **empty** committed template: universe `ok=False`
(`evaluated family universe is empty`) and partition `ok=True` but
`evaluation_ready=False`, with the message
`holdout partition 'holdout' is empty or missing: inventory-only, not
evaluation-ready` (an empty frozen universe has no coverage gaps, but an empty
holdout is never evaluation-ready). Once a contributor freezes a non-empty, all-known
`evaluated_families` subset and fills non-overlapping splits covering it with a
non-empty holdout, the same command prints universe `ok=True` and
`evaluation_ready=True`.

**B. Sanity-check a gold-label file** (family ids exist; evidence belongs to
declared families; chunk refs + spans present):

```bash
cd "/root/code/Super Rag/knowledge-os"
PYTHONPATH='evals:packages:apps/api:apps/worker:.:tests:tests/fixtures' \
  APP_ENV=test LOG_JSON=false \
  /root/ko-venv/bin/python3 - <<'PY'
import json
manifest = json.load(open('docs/graph/G5-DATASET-MANIFEST.json'))
known_family_ids = set(manifest['family_of'].values())
rows = [json.loads(line) for line in open('docs/graph/G5-REAL-GOLD-TEMPLATE.jsonl') if line.strip()]
problems = []
for r in rows:
    for fid in r.get('family_ids', []):
        if fid not in known_family_ids:
            problems.append(f"{r.get('qid')}: family_id not in manifest -> {fid}")
    for ev in r.get('evidence', []):
        if ev.get('family_id') not in known_family_ids:
            problems.append(f"{r.get('qid')}: evidence family_id not in manifest -> {ev.get('family_id')}")
        if not ev.get('chunk_id') or not ev.get('document_id'):
            problems.append(f"{r.get('qid')}: evidence missing chunk_id/document_id")
        sp = ev.get('span', {})
        if 'start' not in sp or 'end' not in sp:
            problems.append(f"{r.get('qid')}: evidence missing span.start/span.end")
print('rows =', len(rows), 'problems =', len(problems))
for p in problems:
    print('-', p)
PY
```

## 4. What stays empty / illustrative (do NOT invent)

- `G5-FAMILY-PARTITION-TEMPLATE.json` — `evaluated_families`, `splits.dev`,
  and `splits.holdout` are **empty**; `common_root` is the portable
  `<G5_DATASETS_ROOT>` placeholder (no machine-specific absolute path).
- `G5-REAL-GOLD-TEMPLATE.jsonl` — exactly **one** row, `label_source` =
  `synthetic-illustrative`, with placeholder `<...>` tokens so no real label is implied.

> **Note on verification:** commands A and B are meant to be run by contributors
> against **their own authored** partition/gold files. Running command A against
> the committed empty template correctly reports universe `ok=False`
> (`evaluated family universe is empty`) plus partition `ok=True` but
> `evaluation_ready=False` (empty frozen universe + empty holdout is
> inventory-only) — that is the
> *expected* blank-form behavior, not a bug. Running command B against the
> committed placeholder template correctly reports 2 problems
> (`family_id not in manifest` for the `<...>` placeholder ids) — also expected,
> since the template's ids are illustrative tokens, not real manifest families.
- No real split assignments, no real results, no quality numbers.
