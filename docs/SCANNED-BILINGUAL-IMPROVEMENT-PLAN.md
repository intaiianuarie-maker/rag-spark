# Scanned, two-column and bilingual documents: improvement plan

Prepared 2026-11-03 on branch `cleanup/portable-install` (uncommitted tree). Status: **proposal only**. No code, config or deployment was changed. Tesseract is **not installed** on the machine where this was prepared, and neither is Docling or lingua, so no real OCR was run. Scratch experiments live outside the repo (paths in §8).

Labels: **VERIFIED** means I read the code or ran it. **UNVERIFIED** means it is inferred, assumed, or depends on a tool that is not available here.

## 1. Current behaviour (evidence)

### 1.1 OCR path for scanned pages

| # | Claim | Evidence | Status |
|---|---|---|---|
| 1 | Default OCR languages are `ron+eng+por`, PSM 3, OEM 1 (LSTM), timeout 60 s per page, `OMP_THREAD_LIMIT=2` | `packages/ingestion/adapters/tesseract.py:19-22`, `:208-219` | VERIFIED |
| 2 | The worker passes `OCR_LANGUAGES` (default `ron+eng+por`) and `OCR_RENDER_DPI` (300). It does **not** pass `max_pages` or `psm`, so OCR is capped at 50 pages per document; pages after 50 go to `failed_pages` | `apps/worker/jobs/ingest_pipeline.py:1443-1448`; `packages/ingestion/adapters/pdf_ocr.py:99`, `:154`, `:182-183` | VERIFIED |
| 3 | The worker imports `ingestion.ocr.process_pdf_with_ocr`, a re-export of `adapters/pdf_ocr.py` | `ingest_pipeline.py:62`; `packages/ingestion/ocr.py:5-12` | VERIFIED |
| 4 | OCR runs only when `OCR_ENABLED` (default **true** in the worker) and the text-layer report finds lost pages. Inside it, only pages classified `OCR_REQUIRED` (fewer than 20 chars / 4 words of native text plus an image) are OCR'd | `ingest_pipeline.py:1436-1441`; `pdf_ocr.py:39-56`, `:117-125` | VERIFIED |
| 5 | Tesseract TSV is parsed at word level (level 5). Words are grouped into line elements by `(block_num, par_num, line_num)` in **TSV emission order** (dict insertion order). Each line element carries a bbox and `metadata.extraction` = a dict (method, version, requested_languages, confidence, source_spans) | `tesseract.py:81-128`, `:142-189` | VERIFIED |
| 6 | Render + Tesseract run **synchronously inside an `async def`** (`subprocess.run`, no `asyncio.to_thread`). The ingest lease heartbeat runs as a task on the **same event loop**, default lease 120 s. A long OCR run therefore blocks lease renewal | `pdf_ocr.py:154-171`; `tesseract.py:222`; `apps/worker/jobs/ingest_worker.py:337-360`, `:436-438`; `packages/ingestion/adapters/redis_queue.py:100` | VERIFIED (code). That this actually causes reclaim or duplicate processing in production is UNVERIFIED |
| 7 | The worker image installs `tesseract-ocr`, `-eng`, `-ron` and `-por` from distro packages | `apps/worker/Dockerfile:17-20` | VERIFIED. Whether these are the `tessdata_best` models the Tesseract plan asked for is UNVERIFIED (distro packages usually ship the standard `tessdata`) |

### 1.2 Two-column reading order

| # | Claim | Evidence | Status |
|---|---|---|---|
| 8 | `_merge_reading_order` inserts each OCR element before the first element in `out` whose `y0` is larger. `out` already contains the OCR elements inserted so far, so on an OCR-only page this works as an insertion sort by `y0`. Tesseract's block order is thrown away and the two columns interleave line by line | `pdf_ocr.py:72-90` | **VERIFIED by running it**: a synthetic PSM-3-style TSV (block 1 = left column, block 2 = right column) goes through the real `parse_tsv_words` → `build_line_elements` → `_merge_reading_order`. The adapter output is `RO1-L1, RO1-L2, RO1-L3, EN2-L1, …` (column-correct). After the merge it is `RO1-L1, EN2-L1, RO1-L2, EN2-L2, …` (interleaved). Script: `scratchpad/plan/two_col_order.py` |
| 9 | PSM 3 runs Tesseract's automatic page layout analysis, which normally emits one block per column, in column order. So the adapter's order is already column-correct when the layout analysis works | Tesseract documentation and general knowledge | UNVERIFIED here (no Tesseract binary). Known failure cases: narrow gutters, full-width headings between columns, and tables split into many blocks |
| 10 | PSM 6 ("single uniform block") reads across both columns line by line, so it is worse for two-column pages. PSM 4 assumes a single column | Tesseract documentation | UNVERIFIED here. The Tesseract plan (§3 step 4) proposed benchmarking PSM 3 against PSM 6. This plan says PSM 6 must not become the default for multi-column scans |
| 11 | Native-text pages go through `parse_with_precedence` (Docling → pypdf → pdfplumber). The earlier claim was that Docling handles the layout. The worker pins `docling-slim[convert-core,format-pdf-docling,feat-chunking]==2.134.0`, described as **torch-free**, and the adapter uses `NativePdfPipelineOptions` | `apps/worker/requirements.txt:19-24`; `packages/ingestion/adapters/docling.py:106`, `:141-145` | Pins and options VERIFIED. Whether the torch-free native pipeline runs a layout model and orders native two-column text correctly is **UNVERIFIED**, so "only the OCR path is suspect" is not proven (see ticket T11) |

### 1.3 Language

| # | Claim | Evidence | Status |
|---|---|---|---|
| 12 | Document language comes from `tag_language`: a declared language always wins (PD-008); otherwise the text of all elements is run through detection | `packages/ingestion/metadata.py:102-121`; `docs/LANGUAGE_POLICY.md` | VERIFIED |
| 13 | **On the OCR path, the detected language is passed in as a declaration.** `process_pdf_with_ocr` detects the language from the first 20 merged elements, and the worker puts that value into `ParsedDocument.language`, which PD-008 then treats as declared. With interleaved columns, those 20 elements are a mix of both languages | `pdf_ocr.py:190-193`; `ingest_pipeline.py:1473-1475` | VERIFIED. This contradicts the Tesseract plan (`TESSERACT-INTEGRATION-PLAN.md` §2 row `metadata.py`: "do not declare `ro` merely because `ron` was requested") |
| 14 | Chunks get the document language. `_make_chunk` writes `metadata["language"] = language` (the document value), and the worker writes `ChunkRow.language = result.language` | `packages/ingestion/chunk.py:104-173`, `:185`; `adapters/llamaindex_chunker.py:130-208`; `adapters/docling_chunker.py:303`, `:316`; `ingest_pipeline.py:1622` | VERIFIED |
| 15 | **A per-chunk language column already exists** (`ChunkRow.language`, `String(16)`). Per-chunk language needs **no schema migration** | `packages/ingestion/persistence.py:128` | VERIFIED |
| 16 | Index records are built as `{"language": result.language, **c.metadata, …}`, so the chunk's own metadata wins. The FTS stemming config (`tsv_cfg`) and the Qdrant payload `language` both come from that merged value | `ingest_pipeline.py:1665-1668`, `:1787-1791`; `packages/retrieval/adapters/pg_fts.py:113`, `:140`; `adapters/qdrant_store.py:673-674` | VERIFIED. Consequence: English paragraphs in a document tagged `ro` are stemmed with the `romanian` config today |
| 17 | **Ask applies no language filter.** `AnswerEngine` calls `orchestrator.retrieve(query, acl_scope, mode, context_top_k)` with no `extra_filter`. The FTS query uses each row's own `tsv_cfg` when no filter is set | `packages/answer/engine.py:498-503`, `:1213-1218`; `pg_fts.py:206-215` | VERIFIED |
| 18 | **Search does apply a language filter** when the caller sets one (`SearchFilter.language` → `ld.language` / `c.language` / Qdrant `language`) | `apps/api/app/search/search.py:64-70`, `:127-129`; `pg_fts.py:197-199`, `:288-290`, `:358-360`; `qdrant_store.py:340-341`; `packages/retrieval/orchestrator.py:744-745` | VERIFIED |
| 19 | Stages 11/12 do not read chunk language metadata. Stage 11's cross-language checks detect the language from claim and sentence text | `packages/answer/verification.py:802-812`, `:927-984`; no `language` metadata reads in `packages/answer` (grep) | VERIFIED |
| 20 | Language detection is cheap: 200 chunks in 0.03 s locally. Locally, lingua is **not installed** and the stopword fallback detected the short diacritic-free Romanian sentence "Plata se face in termen de treizeci de zile" as `en` | scratch run; `metadata.py:20-35` | VERIFIED locally. With lingua 2.2.0 (pinned in `apps/worker/requirements.txt:17`) the result is UNVERIFIED |

### 1.4 Vision, multimodal embedding, tables, charts

| # | Claim | Evidence | Status |
|---|---|---|---|
| 21 | Vision is off by default (`VISION_ENABLED=false`). `VISION_MAX_PAGES=5`, `VISION_DPI=150`, `VISION_IMAGE_BUDGET_BYTES=5 MiB`. `_env_int` rejects values below 1 and falls back to the default, so **`0` cannot mean "no cap"** | `ingest_pipeline.py:87-119` | VERIFIED |
| 22 | Vision only sends the text layer's **lost pages**, sorted, the lowest `VISION_MAX_PAGES` first. It stops at the first page that would push the cumulative PNG bytes over the budget. Skipped pages are **only logged** (`vision_budget_stop`), not disclosed on chunks | `ingest_pipeline.py:174-179`, `:190`, `:222-235` | VERIFIED |
| 23 | Calls are sequential, one page at a time: free Gemma 4 26B A4B first, then the paid route; `max_tokens=1024`, `temperature=0`. Sensitive workspaces raise before any HTTP call. A retried job (`attempts > 1`) skips vision entirely | `ingest_pipeline.py:213-268`, `:300-318`; `packages/models/vision.py:25-27` | VERIFIED |
| 24 | There is also a separate per-image cap: `MAX_IMAGE_PAYLOAD_BYTES = 5 MiB` | `packages/models/vision.py:27` | VERIFIED |
| 25 | **The multimodal page embedding reuses `VISION_MAX_PAGES` and the byte budget, but embeds the first N pages of every PDF** (not only lost pages) | `ingest_pipeline.py:110-115`, `:400-424` | VERIFIED. Removing the vision page cap therefore also removes it for multimodal embeddings, unless the two are split |
| 26 | Vision elements are labelled `[Model-generated page description, page N]` with `metadata.extraction="vision"`. The chunker never mixes them with document text | `ingest_pipeline.py:283-296`; `chunk.py:58-70` | VERIFIED |
| 27 | Stage 11 rule 8: a citation to a vision chunk counts only when the same claim also has a surviving non-vision citation; otherwise it is rejected with `MODEL_GENERATED_ONLY` | `packages/answer/citation_integrity.py:90`, `:638-662`; `packages/answer/models.py:71-82`; tests in `tests/unit/test_vision_citation_policy.py` | VERIFIED |
| 28 | Scanned tables come out as plain Tesseract lines. There is no cell structure and no `TABLE` element type on the OCR path. Native PDFs get `structured_table` from Docling only with the `tableformer`/`structured` profile, which needs the full Docling ML stack | `tesseract.py:169-187`; `docling.py:18-45`, `:124-145`, `:313` | VERIFIED (code). Whether the `tableformer` profile can run in the torch-free image is UNVERIFIED (it would most likely fall back to the native profile) |
| 29 | Existing scanned fixtures are 96×24 dummy images with no real text. They only test unreadability and disclosure | `tests/fixtures/formats/_generate_formats.py:98-125`, `:233-258` | VERIFIED |

### 1.5 What `TESSERACT-INTEGRATION-PLAN.md` already decides (followed here)

- No Docling ML stack is enabled implicitly. No OCRmyPDF, no Qwen, DeepSeek or Paddle OCR, no runtime model downloads (§1, §3, §9).
- PSM 3 is to be benchmarked against PSM 6, with one default profile and no retries across all modes (§3.4).
- Whitespace in TSV output must not be treated as table structure (§3.5).
- Document-dominant language first, with page language kept in metadata for mixed documents (§5). This plan extends that to per-chunk language.
- Multicolumn text and tables are listed as acceptance cases (§11).
- Vision is a later extension. It must never silently correct OCR, and disagreements become review signals (§13).

## 2. Decisions the user must make

| ID | Decision | Options | Recommended default |
|---|---|---|---|
| D1 | **Vision page cap** | (a) keep 5; (b) remove it for vision only; (c) remove it for vision and multimodal embedding | **(b)**. Remove the cap for vision on lost pages only, and give multimodal embedding its own `MULTIMODAL_MAX_PAGES` (default 5). Multimodal currently embeds every PDF's first pages, so removing its cap multiplies spend on every digital PDF too |
| D2 | **Byte budget after the cap is removed** | (a) keep 5 MiB, which then becomes the real cap (§5.1); (b) raise it to a runaway guard (e.g. 200 MiB per document); (c) replace it with a per-document page count plus a per-job time ceiling | **(b)**. Keep a byte budget as the runaway guard, set it to 200 MiB, add `VISION_MAX_SECONDS` per job (e.g. 900 s), and disclose budget-skipped pages on chunks. The per-image 5 MiB cap stays |
| D3 | **Language per chunk vs PD-008** | (a) a declared document language also wins for every chunk, with the detected language stored as `language_detected`; (b) per-chunk detection overrides a declaration for chunks of at least N words | **(a)**. It keeps `LANGUAGE_POLICY.md` intact. Most bilingual scans have no real declaration once T4 stops the OCR path pretending its detection is one. Revisit (b) only if a connector declares languages for bilingual sources |
| D4 | **Search language filter on bilingual documents** | (a) filter matches the chunk language (the natural result after T5); (b) filter matches the document language | **(a)**. A `language=en` search returns the EN chunks of a bilingual document, not every chunk of a document tagged `ro`. This is a behaviour change, so it gets a release note |
| D5 | **Scanned tables** | (A) Docling OCR + TableFormer; (B) table grid built from Tesseract word geometry (no new dependencies); (C) vision table extraction, model-generated only | **(B)** for ruled or clearly aligned tables. (C) only as labelled support when D1 enables vision. Reject (A) for now: it needs torch (~1.5 GB, against the `docling-slim` choice) and contradicts the Tesseract plan |
| D6 | **Chart answerability** | (a) charts stay unanswerable except for text printed on them; (b) also allow vision-extracted chart data as labelled, unverified context; (c) let vision data alone support a verified answer | **(a) plus (b)**. Text printed on the chart (title, axis labels, data labels) that OCR can read is document text and citable. Vision-extracted values may appear only as labelled model-generated context and never alone in a verified claim. Do **not** choose (c) |
| D7 | **Change `MODEL_GENERATED_ONLY`?** | keep / relax | **Keep it unchanged.** Relaxing it changes the trust contract of verified answers. This plan does not argue for a change. If anyone proposes one, the user, as product owner, decides, with the project's current reviewer/overseer signing off, after an eval showing chart-value accuracy on a labelled set. The rule lives in `citation_integrity.py:638-662` and is guarded by `test_vision_citation_policy.py` |
| D8 | **OCR page cap (50)** | keep; make it configurable via `OCR_MAX_PAGES` (the name already proposed in the Tesseract plan, §7) | **Make it configurable, default 50**, but only after T2 (non-blocking OCR). Raising it before T2 lengthens event-loop blocking |

## 3. Tickets

Template per project rule: goal, files, failing test first (✅ Tests), ☑ Done when, risk, model. Model choices follow the project's Artificial Analysis Pareto split: Haiku xhigh for mechanical work, Sonnet high for implementation, Opus medium for trust or cost decisions.

### T0: Decision record for D1–D8 (Opus medium)
- **Goal:** record the user's choices for D1–D8 in `rag-fork-solution/DECISIONS.md` (or the project's current decision log) before T6, T8 and T9 start.
- **Files:** the decision log only.
- ✅ **Tests:** none (decision only).
- ☑ **Done when:** each of D1–D8 has a chosen option, an owner and a date.
- **Risk:** low. Skipping it risks surprise vision spend (D1/D2) or a policy drift (D7).

### T1: Keep Tesseract block order on OCR pages (Sonnet high)
- **Goal:** fix the root cause in `_merge_reading_order`. On a page with no native bbox elements, keep OCR elements in adapter order (Tesseract block order). On mixed pages, compare each OCR element only against **native** elements, never against OCR elements already inserted, and insert OCR blocks as contiguous units keyed by the top of their block.
- **Files:** `packages/ingestion/adapters/pdf_ocr.py:72-90`.
- ✅ **Tests (write first, must fail today):** add to `tests/unit/test_pdf_ocr_reading_order.py` the two-column case from `scratchpad/plan/two_col_order.py`. The expected order is the left column, then the right column. The four existing tests in that file must still pass unchanged.
- ☑ **Done when:** the new test passes, the existing reading-order tests pass, and `test_ocr_adapters.py` passes.
- **Risk:** low. Mixed native+OCR pages are rare (§3 of the Tesseract plan already sends them to review).

### T2: Make OCR non-blocking and configurable (Sonnet high)
- **Goal:** run `render_pdf_page` and `ocr_page_image` through `asyncio.to_thread`, one page at a time (PDFium is not used concurrently), so the lease heartbeat keeps running. Pass `max_pages=int(os.environ.get("OCR_MAX_PAGES", "50"))` and `psm` from `OCR_PSM` (default 3) from the worker.
- **Files:** `packages/ingestion/adapters/pdf_ocr.py:152-179`, `apps/worker/jobs/ingest_pipeline.py:1443-1448`, `.env.example`.
- ✅ **Tests:** a unit test where a fake `ocr_page_image` sleeps 0.2 s per page for 5 pages while a concurrent coroutine counts ticks every 0.05 s. It must see at least 10 ticks; today it sees roughly 0. Plus a test that `OCR_MAX_PAGES=2` marks page 3 as failed.
- ☑ **Done when:** both tests pass and the existing OCR and worker unit tests are green.
- **Risk:** medium. Thread offload changes timing in worker tests. Killable child-process isolation (Tesseract plan §7) stays a separate, larger ticket; mark the gap with a `ponytail:` comment.

### T3: Real bilingual two-column scanned fixture plus Tesseract integration test (Sonnet high)
- **Goal:** add a generated, image-only fixture (§7) to the existing generator, with ground truth, and an integration test that runs real Tesseract. The test skips when `shutil.which("tesseract")` is None and runs in the worker image.
- **Files:** `tests/fixtures/formats/_generate_formats.py` (new `pdf_scanned_bilingual_two_column()`), generated `tests/fixtures/formats/pdf_scanned_bilingual_2col.pdf`, `tests/fixtures/formats/pdf_scanned_bilingual_2col.truth.json`, `tests/integration/test_ocr_two_column_bilingual.py`.
- ✅ **Tests:** with real Tesseract and PSM 3: (1) after T1, every RO line precedes every EN line on page 1; (2) per-language character error rate is at most 2% against the truth file (the Tesseract plan's target); (3) the table cell strings `1.250,00` and `3.750,00` appear verbatim.
- ☑ **Done when:** the test passes in the worker container and is skipped (not failed) where Tesseract is missing. It also records seconds per page (p50) for §5.2.
- **Risk:** the font must be available in the container (use DejaVu Sans, which has `ș ț ă î â`). It also exercises the first real PSM 3 column verification (claim 9).

### T4: Stop declaring the OCR-detected language (Sonnet high)
- **Goal:** follow PD-008. `ParsedDocument.language` on the OCR path stays `None`. The OCR report's detected language moves to `metadata["extraction_report"]["languages"]` (already present) and nowhere else. `tag_language` then detects from the **whole** text, not just the first 20 interleaved elements.
- **Files:** `apps/worker/jobs/ingest_pipeline.py:1469-1477`; optionally drop the 20-element sample in `pdf_ocr.py:191`.
- ✅ **Tests:** a unit test where an OCR-path document (mocked `process_pdf_with_ocr`) ends with `extract_metadata(...).extra["declared"] is None` and `detected` set. Today `declared` equals the OCR guess.
- ☑ **Done when:** that test passes and `test_pd008_language.py`, `test_ingestion_language.py` and `test_vision_cost_controls.py` (`test_romanian_without_diacritics_stays_ro_after_vision`, `test_declared_language_is_not_overridden_by_pin`) still pass.
- **Risk:** low. The FTS config for OCR documents may change from the guess to whole-text detection.

### T5: Per-chunk language (Sonnet high; depends on D3 and T4)
- **Goal:** each chunk's `metadata["language"]` and `ChunkRow.language` come from the chunk's own text when the document has no declared language (D3a). A chunk with too little signal (fewer than about 8 words, or detection returns `None`) falls back to the document language. Always store `language_detected` for observability. To keep a chunk from mixing languages, `_sections` starts a new group when an element's language hint differs from the current group's. For OCR, the hint is computed **per Tesseract block** (all lines of a block joined), not per line: lines are too short (claim 20).
- **Files:** `packages/ingestion/chunk.py:58-103`, `:176-200` (shared by the builtin and the default `llamaindex` backends via `sections_from_elements` / `make_chunk`); `packages/ingestion/adapters/tesseract.py:142-189` (block-level `metadata["language_hint"]`); `packages/ingestion/adapters/docling_chunker.py:303`, `:316`; `apps/worker/jobs/ingest_pipeline.py:1622` (`language=chunk.metadata.get("language", result.language)`). **No migration** (claim 15).
- ✅ **Tests (write first):** (1) `chunk_elements` with 3 RO-block lines and 3 EN-block lines on one page produces at least 2 chunks, with languages `ro` and `en`. (2) A declared `ro` document produces chunks with `language == "ro"` and `language_detected == "en"` for the EN chunk (D3a). (3) An index record for the EN chunk gets `tsv_cfg == "english"` (`pg_fts` unit test with a fake session). (4) `SearchFilter(language="en")` over a bilingual document returns only the EN chunk (D4a).
- ☑ **Done when:** tests 1–4 pass, the existing chunker and FTS unit tests pass, and Stage 11/12 tests are unchanged (claim 19: they do not read chunk language).
- **Risk:** medium. More, smaller chunks on bilingual pages. Short RO text without diacritics may be misdetected (claim 20), and the fallback rule covers that. Citations are unaffected: page and bbox provenance stay per chunk.

### T6: Vision cap per D1/D2 (Sonnet high; depends on T0)
- **Goal:** implement the chosen D1/D2. For the recommended defaults: `VISION_MAX_PAGES` accepts `0` = no page cap (vision only); new `MULTIMODAL_MAX_PAGES` (default 5) replaces the shared constant at `ingest_pipeline.py:403`; `VISION_IMAGE_BUDGET_BYTES` default becomes 200 MiB; new `VISION_MAX_SECONDS` stops the loop. Skipped pages (budget, time or cap) go into the chunk disclosure (`lost_regions`, reason `vision_skipped`), not only the log.
- **Files:** `apps/worker/jobs/ingest_pipeline.py:95-119`, `:190`, `:222-236`, `:400-424`; `.env.example`; `tests/unit/test_vision_cost_controls.py`, `tests/unit/test_ingest_multimodal_step.py`.
- ✅ **Tests:** (1) with `VISION_MAX_PAGES=0` and 12 lost pages, 12 pages are sent; (2) the multimodal step still embeds at most `MULTIMODAL_MAX_PAGES`; (3) budget and time stops are disclosed with the page list. The existing test `test_many_lost_pages_are_sorted_and_capped_at_max_pages` must be kept with an explicit cap.
- ☑ **Done when:** the new and existing vision and multimodal tests pass, and the sensitive-workspace and retry-skip tests are unchanged.
- **Risk:** cost and latency (§5). The retry-skip rule (`ingest_pipeline.py:309`) means a long vision run that fails is not redone. Accepted, already marked `ponytail:`.

### T7: OCR table structure from word geometry (Sonnet high; depends on D5, T3)
- **Goal:** detect table regions on OCR pages from Tesseract word boxes. A region is a table when at least 3 consecutive lines share at least 2 aligned column x-ranges (gap clustering). Emit an `ElementType.TABLE` element with `metadata["structured_table"]` using the existing `StructuredTable`/`StructuredCell` contract (`docling.py:18-45`), `profile="ocr_geometry"`. Lines that are not detected as a table stay plain `TEXT`, as today (the Tesseract plan forbids whitespace-as-structure, so alignment must come from boxes, not spaces). Mark it `ponytail:` as a heuristic for ruled or aligned tables; ruled-line detection from the raster is the upgrade path.
- **Files:** new function in `packages/ingestion/adapters/tesseract.py` (or a small `ocr_table.py` next to it); call from `ocr_page_image`.
- ✅ **Tests:** (1) a synthetic TSV for a 3×3 aligned table produces one TABLE element with 3×3 cells and the correct cell text; (2) two-column body text (not a table) produces **no** TABLE (false-positive guard); (3) with real Tesseract, the T3 fixture's table produces 3 rows.
- ☑ **Done when:** all three pass and the chunker keeps TABLE elements intact (existing table tests).
- **Risk:** medium-high. False positives on two-column prose are the main risk, and test 2 guards against them. Table cells feed citations, so a wrong grid misattributes values. Unruled tables stay text only, and that is documented.

### T8: Chart policy implementation (Opus medium for the prompt/policy text, Sonnet high for code; depends on D6/D7)
- **Goal (recommended D6 a+b):** (1) For pages vision already handles, change the prompt to ask separately for "text printed on the chart" and "values read from the chart". Both stay `extraction="vision"`, so Stage 11 rule 8 is unchanged. (2) Chart text on scanned pages is already OCR'd by Tesseract, so it is citable document text; nothing to change there. Raster charts inside native-text pages are not OCR'd today (claim 4: the page is `NATIVE_TEXT`). Note this as a known gap; do not fix it in this plan.
- **Files:** `apps/worker/jobs/ingest_pipeline.py:244-258` (prompt only).
- ✅ **Tests:** keep all of `test_vision_citation_policy.py` green, and add one more case: a claim citing only a chart-values vision chunk is rejected with `MODEL_GENERATED_ONLY`.
- ☑ **Done when:** policy tests pass and the answer UI shows chart values only as labelled model-generated context.
- **Risk:** low for code. The trust risk would only arise under D7 "relax", which is not recommended.

### T9: Scanned-page disclosure for unsupported layouts (Sonnet high)
- **Goal:** when the T7 detector finds a table it cannot structure, or Tesseract's mean confidence on a page falls below a threshold to be set from T3 numbers, add a `layout_uncertain` page note to the chunk disclosure, in line with the Tesseract plan's "classify uncertain layouts honestly" (§11).
- **Files:** `packages/ingestion/adapters/pdf_ocr.py` (report), `ingest_pipeline.py` (`lost_regions` assembly at `:1488-1498`).
- ✅ **Tests:** a low-confidence fake OCR page produces a `layout_uncertain` entry on the chunks of that page.
- ☑ **Done when:** that test passes and the existing disclosure tests are unchanged.
- **Risk:** low.

### T10: Documentation updates (Haiku xhigh; after T1–T7)
- **Goal:** update `docs/ingestion/FORMAT-MATRIX.md` (scanned two-column, bilingual, OCR tables), add a per-chunk clause to `docs/LANGUAGE_POLICY.md` that keeps PD-008 unchanged, and add an `.env.example` entry for each new variable.
- ✅ **Tests:** `test_format_matrix.py` still passes.
- ☑ **Done when:** the docs match the shipped behaviour and every claim cites a test.
- **Risk:** low.

### T11: Verify native two-column order through torch-free Docling (Sonnet high, spike)
- **Goal:** settle claim 11. Generate a native-text two-column PDF (reuse `_generate_formats.py` `text_op`) and run it through `parse_with_precedence` in the worker image.
- ✅ **Tests:** a reading-order assertion like T1's, for native text.
- ☑ **Done when:** the result is recorded. If it fails, open a separate ticket; do not expand this plan.
- **Risk:** none (spike).

## 4. Ordering and dependencies

```
T0 (decisions) ──► T6 (vision cap) ──► T8 (charts)
              └──► T5 (needs D3/D4) ; T7 (needs D5)
T1 (merge fix) ─┐
T2 (non-blocking OCR) ─┼──► T3 (real fixture, worker image) ──► T7, T9
T4 (no fake declaration) ──► T5 (per-chunk language)
T11 (spike) independent
T10 last
```

- **Independent, start now:** T1, T2, T4, T11. They are small and need no decision.
- **Must go first:** T0 for D1/D2 (vision spend) and D3 (per-chunk language versus PD-008). The language "schema" turns out to be **already in place** (`ChunkRow.language`, claim 15), so no migration blocks T5. Only the policy decision does.
- T3 is needed before T7 and T9 thresholds can be set from real numbers.
- T6 before T8 (the chart prompt only matters when vision runs on more pages).

## 5. Cost and latency estimates

All figures are estimates unless marked measured. Assumptions are stated for each.

### 5.1 Vision (Gemma 4 26B A4B via OpenRouter, free → paid)
- **Paid price** (repo snapshot `docs/evidence/openrouter-catalogue-2026-10-16.json`, 3 of 13 endpoints sampled): prompt $0.042–0.07 per M tokens, completion $0.22–0.34 per M tokens. Prices change; UNVERIFIED for today.
- **Tokens per page (assumed):** about 40 prompt-text tokens, 256–1,120 image tokens (Gemma image-token count for Gemma 4 is UNVERIFIED), and up to 1,024 completion tokens (`max_tokens`, `ingest_pipeline.py:265`).
- **Paid cost per page (upper bound):** 1,200 × $0.07/M + 1,024 × $0.34/M ≈ **$0.0004 per page**, so about **$0.43 per 1,000 pages**. The free route costs $0 while it is not exhausted. Spend is small; the cap matters more for **latency, rate limits and data egress** than for money.
- **Latency (assumed, UNVERIFIED):** 5–20 s per page, sequential. With no page cap, a 100-page scanned PDF adds about **8–33 minutes** per ingest job. The lease heartbeat keeps the claim alive while vision runs (vision awaits HTTP, so the loop is not blocked), but queue throughput falls by the same amount for one worker.
- **What the byte budget does after the page cap is removed:** it becomes the **effective page cap**. It counts cumulative PNG bytes per document (`ingest_pipeline.py:221-236`) and stops at the first page that would exceed 5 MiB. Measured: the synthetic, clean A4 page renders to 99 KB at 150 DPI, which would allow about 50 pages. Real grey or colour scans are noisy and usually much larger; assuming 0.3–1.5 MB per page, 5 MiB allows only about **3–17 pages**. In practice, removing the page cap without raising the budget changes little for real scans. Hence D2 recommends raising it to a runaway guard (200 MiB ≈ 130–650 real pages) and adding a time ceiling.
- **Multimodal embedding (google/gemini-embedding-2):** the catalogue lists `image: 0.00000045`, but whether that is per image or per token is UNVERIFIED. If D1 (c) is chosen, every PDF's pages are embedded, not just scans. Keep it separate (D1 b).

### 5.2 OCR (Tesseract, CPU)
- **Rendering, measured on this Windows laptop** (synthetic page, pypdfium2 5.x): 0.53 s per page at 300 DPI (311 KB PNG), 0.47 s at 150 DPI.
- **Tesseract LSTM with 3 languages, `OMP_THREAD_LIMIT=2`:** assumed **4–15 s per A4 page**, UNVERIFIED (no binary here). Loading 3 language models costs time on every call because each page spawns a new process. T3 records real p50 and p95 on the target host (the Tesseract plan says not to extrapolate).
- **50-page cap:** about 3.5–12.5 minutes per document. Before T2, the whole run blocks the event loop, against a 120 s lease. Hence T2 comes before raising `OCR_MAX_PAGES`.
- **Per-chunk language detection:** measured 0.03 s per 200 chunks (stopword fallback). lingua is slower but still milliseconds per chunk (UNVERIFIED with lingua).
- **T7 geometry tables:** pure Python over word boxes, which is negligible next to OCR.

## 6. Test plan

**Fixture (synthetic, nothing downloaded):** one A4 page at 300 DPI, image only (no text layer). Left column: 5 Romanian lines with `ă î ț ș`. Right column: 5 English lines that translate them. Under both columns: a ruled 3×3 table with RO/EN headers and the numbers `1.250,00` and `3.750,00`. A prototype generator is at `scratchpad/plan/make_fixture.py`; it uses Pillow and Arial on Windows, and the repo version should use DejaVu Sans. The repo's classifier rates it `ocr_required` (run here). Add a second page with a full-width heading above the two columns, the known PSM 3 hard case, and one native-text page so the document is mixed.

**Success looks like:**
1. Reading order: every left-column line precedes every right-column line (T1, T3).
2. Character error rate is at most 2% per language on the clean fixture (T3).
3. Chunks: at least one `ro` chunk and one `en` chunk on page 1, with no chunk mixing them; `ChunkRow.language` and the FTS `tsv_cfg` match (T5).
4. Ask with "When is payment due?" (EN) and "În cât timp se face plata?" (RO) retrieves the matching-language chunk, cites page 1, and passes Stage 11/12 (live check in the worker/API stack, run by the user).
5. Table: one TABLE element with 3 rows and the exact amount strings (T7).
6. Vision on (if D1): the vision chunk exists, is labelled, and cannot alone support the amount claim (`MODEL_GENERATED_ONLY`).

**Must not regress:**
- `tests/unit/test_pdf_ocr_reading_order.py` (4 tests), `test_ocr_adapters.py`
- `test_pd008_language.py`, `test_ingestion_language.py`
- `test_vision_cost_controls.py`, `test_ingest_vision_step.py`, `test_ingest_vision_gateway.py`, `test_ingest_multimodal_step.py`
- `test_vision_citation_policy.py` (`MODEL_GENERATED_ONLY`)
- `test_format_matrix.py` (scanned fully-lost and partial detection when OCR is off)
- Digital-PDF behaviour with OCR off; Ask with no language filter (claim 17); and Search filter semantics for single-language documents, which must be identical before and after.

## 7. Not verified

1. PSM 3 emits column-correct block order on real two-column scans (claim 9). No Tesseract binary here; T3 settles it.
2. PSM 6 and PSM 4 behaviour on two-column pages (claim 10).
3. The torch-free Docling native pipeline orders native two-column pages correctly (claim 11); T11 settles it.
4. Distro `tesseract-ocr-ron/eng/por` packages are `tessdata_best` (claim 7).
5. Blocking OCR actually causes lease loss or a duplicate job in production (claim 6). The code path is verified; the incident is not.
6. lingua's accuracy on short Romanian text without diacritics. The local misdetection used the stopword fallback because lingua is not installed here (claim 20).
7. Gemma 4 image-token count, current OpenRouter prices, per-page vision latency, and the unit of the gemini-embedding-2 `image` price (§5.1).
8. Tesseract seconds per page (§5.2), and PNG size per real scanned page at 150 DPI (the 0.3–1.5 MB assumption).
9. Whether the Docling `tableformer` profile can run in the `docling-slim` image (claim 28).
10. Whether the decision log for D1–D8 is `rag-fork-solution/DECISIONS.md` and who the current reviewer/overseer is for this repo.

## 8. Scratch artefacts (outside the repo)

- `C:\Users\CIPRIA~1.STO\AppData\Local\Temp\claude\C--Users-ciprian-stoica-Antigravity\b9298403-bdb0-4f9a-85cb-62541057bfad\scratchpad\plan\two_col_order.py`: reproduces the column interleaving (claim 8).
- `C:\Users\CIPRIA~1.STO\AppData\Local\Temp\claude\C--Users-ciprian-stoica-Antigravity\b9298403-bdb0-4f9a-85cb-62541057bfad\scratchpad\plan\make_fixture.py`: generates the bilingual two-column scanned page and confirms the repo's classifier marks it `ocr_required`.
- `C:\Users\CIPRIA~1.STO\AppData\Local\Temp\claude\C--Users-ciprian-stoica-Antigravity\b9298403-bdb0-4f9a-85cb-62541057bfad\scratchpad\plan\bilingual_two_column_scan.pdf`: the generated fixture (195 KB).
