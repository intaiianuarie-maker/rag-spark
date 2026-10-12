# G5 source inventory & reproducibility manifest (V2)

> Status: **inventory + manifest complete; real paired eval still BLOCKED on
> authored gold labels + a family dev/holdout split.** This document reports
> what source material actually exists and what annotations are **missing**; it
> invents no labels.
>
> Author: dev2 (Stream B) · 2026-10-11 · branch `feat/v2-graph`.

Tooling (stdlib-only, NOT product code):

- `evals/graph_dataset_manifest.py` — SHA-256 source hashing, source-family
  grouping, manifest write/load, partition validation (no family overlap),
  gold-reference validation (no dangling labels).
- `evals/build_graph_manifest.py` — walks the pinned dataset roots and writes
  `docs/graph/G5-DATASET-MANIFEST.json`.
- `tests/unit/test_graph_dataset_manifest.py` — 9 hermetic tests pin the
  hashing, manifest round-trip, partition-overlap rejection, and dangling-label
  rejection.

## Manifest (pinned)

`docs/graph/G5-DATASET-MANIFEST.json` — **1,248 files, 282,355,978 bytes,
869 source families** under `/root/code/Super Rag/datasets/rag-eval` (T2
FinQA + TAT-DQA). Every file carries `size_bytes` + `sha256`; `family_of` maps
each file to its leakage unit. Regenerate with:

```bash
/root/ko-venv/bin/python3 evals/build_graph_manifest.py \
  --datasets-root "/root/code/Super Rag/datasets/rag-eval" \
  --out docs/graph/G5-DATASET-MANIFEST.json
```

### Source-family definition (the leakage unit)

- **FinQA:** `finqa:<company_symbol>:<report_year>` — one annual report and its
  questions are one family. Different years of the same company are **different**
  families (distinct reports); `finqa:metadata` is the shared annotation file.
- **TAT-DQA:** `tatdqa:<raw_file_stem>` — per-report (raw ids are hashes; we do
  **not** guess a company mapping); `tatdqa:metadata` is the shared annotation
  file. This is intentionally conservative: two files are never merged into one
  family on a guess.
- Family count: **867 TAT-DQA + 380 FinQA** families (+ 1 metadata each).

## What is actually available (honest inventory)

| Corpus | Path | Parseable here? | Gold labels present? |
|---|---|---|---|
| FinQA (T2) | `t2-finance-tests-v1/data/FinQA/test` | ✅ JSONL `metadata.jsonl` (1147 rows) | ⚠ no gold **chunk** ids; `context` is pre-extracted text; single `split=test` |
| TAT-DQA (T2) | `t2-finance-tests-v1/data/TAT-DQA/test` | ✅ JSONL `metadata.jsonl` (1144 rows) | ⚠ no gold **chunk** ids; `context` is text+table; single `split=test` |
| FinanceBench | `financebench-noncommercial-v1` | ✅ JSONL (`evidence[].evidence_text`, `doc_name`) | ✅ gold **source** refs (evidence) — but **NON-COMMERCIAL license** |
| EnterpriseRAG-Bench | `enterprise-rag-bench-v1` | ❌ parquet (no reader) | ✅ per its manifest: expected doc ids + gold answers + answer facts (**MIT**, HF-pinned) |
| RAGBench hotpotqa | `ragbench-tests-v1/hotpotqa` | ❌ parquet (no reader) | multi-hop source material (schema via README) |
| RAGBench tatqa/finqa | `ragbench-tests-v1/{tatqa,finqa}` | ❌ parquet (no reader) | table/text QA (schema via README) |
| Belebele / XQuAD | `belebele-v1`, `xquad-en-ro-v1` | ✅/❌ | multilingual reading-comprehension (EN/RO/PT) — relevant to V1 Q9, not graph multi-hop |

**No parquet reader** (`pyarrow`/`fastparquet`) is installed in the test venv and
adding a dependency is out of scope, so parquet corpora are **hashed but not
parsed** here; their schemas are reported from their README/manifest, never
guessed.

## Missing annotations (BLOCKERS for a real paired G5) — reported, not invented

1. **Gold chunk/source references in the chunked corpus.** FinQA/TAT-DQA ship a
   pre-extracted `context` string, not chunk ids in *our* ingestion output. To
   measure EvidenceRecall@K we must map each question to the specific chunks
   (after ingestion) that substantiate it. FinanceBench has evidence text but
   not our chunk ids either.
2. **A source-family dev/holdout partition.** All T2 rows are `split=test`
   today. A leakage-safe dev/holdout split must be authored by family (a report
   + its questions stay together) and validated with
   `validate_partitions` (no family in both splits).
3. **Multi-hop gold structure.** For the graph leg to be *fairly* evaluated, gold
   must mark the multi-hop evidence chain (which chunks require a relation hop).
   hotpotqa is a multi-hop *source* but unread here (parquet).
4. **Confidence-interval inputs.** Per-slice paired statistics need ≥1 example
   per slice and a frozen dataset hash; slices are undefined until (1)–(3) land.

## Integrity guarantees this manifest provides

- **Reproducibility:** a paired run records the manifest (root + per-file
  SHA-256); a later run re-hashes and diffs to detect corpus drift.
- **No invented labels:** `validate_gold_refs` fails any example citing a source
  not present in the manifest (dangling label), and partition validation fails
  any family that appears in both dev and holdout.
- **Honest runtime:** see [`G5-EVAL-REPORT.md`](./G5-EVAL-REPORT.md) — the fake
  harness is mechanism-only; a real run needs the REAL-adapter runner in
  [`G5-REAL-RUNNER-CONTRACT.md`](./G5-REAL-RUNNER-CONTRACT.md).
