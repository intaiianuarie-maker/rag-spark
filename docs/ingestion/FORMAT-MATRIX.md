# Format matrix — what ingestion actually supports

**Source of truth: `packages/ingestion/format_matrix.py`.** This page mirrors
that table and `tests/unit/test_format_matrix.py` fails if they ever drift, so
a cell cannot be advertised here without being proven (or explicitly rejected)
in code. Ticket **T-V1-031**.

The rule this page exists to enforce: **every advertised cell is either proven
against a fixture or explicitly rejected with a typed error.** There is no
third state — in particular no "we advertise it, we just never tested it", and
no "it probably works".

## The matrix

| Cell | Format | Outcome | Proven by | Fixture |
|---|---|---|---|---|
| `pdf_digital` | Digital PDF (has a text layer) | `supported` | `PdfTextParser` | `tests/fixtures/formats/pdf_digital.pdf` |
| `pdf_tables` | Table-heavy PDF | `supported` | `PdfTableParser` | `tests/fixtures/formats/pdf_tables_heavy.pdf` |
| `pdf_scanned` | Scanned PDF (image-only, no text layer) | `partial` | `pdf_text_layer_report` | `tests/fixtures/formats/pdf_scanned.pdf` |
| `docx` | DOCX (Word) | `supported` | `DocxParser` | `tests/fixtures/formats/docx_tables.docx` |
| `xlsx` | XLSX (Excel) | `rejected` | `parser_for_mime` | `tests/fixtures/formats/sheet.xlsx` |
| `image` | Image (PNG / JPEG) | `rejected` | `parser_for_mime` | `tests/fixtures/formats/scan.png` |

A supporting fixture, `tests/fixtures/formats/pdf_mixed_scan.pdf` (one readable
page + one image-only page), proves the *partially* lost case, which is where
the disclosure matters most: the readable text is published, the lost page is
named, and the job still refuses to call itself `ready`.

## What each cell claims

### `pdf_digital` — `supported`

Text is extracted per page with the page number preserved, numbered headings
(`1. Introduction`) become sections, and the first line of page 1 becomes the
title element. **Nothing is disclosed** — the whole document was read.

**Runtime caveat.** Docling is the **primary** PDF parser (T-V1-030). A
deployment without docling falls back to pypdf for this cell, and the fallback
is disclosed on every chunk (`parse.fallback_used = true`) and in the job's
parse report, so a fallback read is never presented as the primary's read.

### `pdf_tables` — `supported`

Ruled tables survive as `TABLE` elements with their rows intact — never
flattened into a text soup — and body text outside a table stays `TEXT`.

**Runtime caveat (read this one).** The claim is a claim about the **table
adapter**. Docling (primary, T-V1-030) preserves table content; pypdf — the
text fallback — returns table rows as plain lines and loses the `TABLE`
structure. A deployment without docling therefore reads this cell as text,
disclosed through `parse.fallback_used`. It is never presented as a table
extraction.

### `pdf_scanned` — `partial`

An image-only page is never read as if it were read:

* **no text is invented** for a page that has none;
* the lost pages are listed, as page numbers, in the disclosure;
* the job ends in **`needs_review`**, never `ready`;
* the disclosure rides on **every chunk** the job publishes, so the answer layer
  can repeat it (see *Disclosure reaches answers* below).

Two shapes, both covered:

| Shape | Behaviour |
|---|---|
| Every page is image-only | Nothing is published. The job is parked `needs_review` with the page list and `chunks = 0`. Indexing an empty document as if it had been read would be the dishonest outcome. |
| Some pages are image-only | The readable text is published and retrievable; the lost pages are disclosed on every chunk; the job still ends `needs_review`. |

**Runtime caveat.** **OCR is not part of this deployment.** This cell cannot be
upgraded to `supported` by configuration — adding OCR is a new decision row in
`DECISIONS.md`, not a config change. Until then the honest answer to "we cannot
read this page" is to say so.

### `docx` — `supported`

Body order is preserved, Title / Heading 1 becomes `TITLE`, lower headings
become sections, and tables stay `TABLE` elements.

**Runtime caveat.** Docling is the primary DOCX parser (T-V1-030); without
docling the cell is served by python-docx and the fallback is disclosed.

### `xlsx` — `rejected`

There is **no XLSX adapter in v1**. An upload is refused with a typed
`unsupported_mime_type` **before any parser sees the bytes** — it is never
half-ingested and never silently dropped.

```
IngestPipelineError('unsupported_mime_type', retryable=False)
```

The refusal is a *refusal of content*, not of corruption: the fixture
`sheet.xlsx` is a valid workbook, and it is still refused.

### `image` — `rejected`

Images are not a v1 document format. PNG and JPEG uploads are refused with the
same typed `unsupported_mime_type` before any parser sees the bytes — an image
is never silently indexed as an empty document.

## Parse budgets, enforced before expansion

The pipeline refuses a document on **four budgets**, all env-tunable, all
enforced *while the parser expands the document* — never by counting elements
after the fact. Counting afterwards is a verdict on work already paid for; a
budget has to stop the work.

| Budget | Env var | Default | Code |
|---|---|---|---|
| Parse wall clock | `INGEST_PARSE_TIMEOUT_SECONDS` | `120` | `parse_time_budget` |
| Peak memory (RSS) | `INGEST_PARSE_MAX_MEMORY_MB` | `1024` | `parse_memory_budget` |
| Expanded text | `INGEST_PARSE_MAX_EXPANDED_CHARS` | `2_000_000` | `parse_expanded_text_budget` |
| Output elements | `INGEST_PARSE_MAX_OUTPUT_ELEMENTS` | `100_000` | `parse_output_budget` |

All four raise `ParseBudgetExceeded` and are parked as **non-retryable** —
retrying a deterministic budget breach can only spend the same budget again.

**Where enforcement happens.** Every PD-001 adapter builds its tree through
`ingestion.normalize.make_element`, which is the single choke point: it calls
`ParseBudgetGuard.observe(text)` per element. The budget therefore trips
*between* element *n* and element *n+1* — expansion stops, the third-party
parser unwinds, and no `ParsedDocument` is ever returned. This is what
"pre-expansion" means, and the tests assert the element count at the moment of
the breach (budget + 1, not "the whole document").

**Two disclosed limits** (stated, not hidden):

1. A budget can only trip where a checkpoint runs. `observe` is the
   pre-expansion checkpoint; `parse_with_budgets` additionally enforces the wall
   clock with a timeout and re-checks the accounting when the parse returns, so
   a parser that blocks *before* emitting anything is still abandoned at the
   deadline.
2. Python cannot kill a thread blocked inside a C call. The timeout **abandons**
   that thread. The abandoned parse is tripped by the guard at its next
   checkpoint — which is what makes abandonment effective for the expansion-bomb
   case this budget exists for — but a call wedged inside a third-party C
   routine keeps its thread-pool slot until it returns.

The old element-count guard (`ensure_element_limit`, `INGEST_MAX_ELEMENTS`) is
**kept** as an independent post-parse backstop. Two guards on the same limit is
not the same as one.

## Disclosure reaches answers, and answers are never confident

The ingestion pipeline writes the disclosure onto every chunk it publishes:

```python
chunk_metadata = {
    **chunk.metadata,
    "parse": {"primary": ..., "used": ..., "fallback_used": ..., "failed": [...]},
    "parse_partial": True,                      # only when regions were lost
    "parse_lost_regions": [{"reason": "no_text_layer",
                            "lost_pages": [2], "page_count": 2,
                            "readable_pages": 1}],
}
```

Retrieval hands that metadata straight back on the retrieved items, and
`AnswerEngine._disclose_source` applies it:

* a **standing** answer over a partially-parsed source is relabelled
  **`needs_review`** (`AnswerStatus.NEEDS_REVIEW`, reason `NEEDS_REVIEW`) and
  the lost regions are appended to `Answer.limitations`;
* confidence is **capped at `MEDIUM`**. `HIGH` claims completeness, and
  completeness is exactly what a partially-read source does not have.

An abstention keeps its own reason (`INSUFFICIENT_EVIDENCE`) — it is already
not a claim to have read anything — but still carries the limitation, so the
user learns the source was only partly readable.

**Metric this ticket pins:** confident answers drawn from an omitted region: **0**.

## Reading the outcome column

| Outcome | Means |
|---|---|
| `supported` | Proven against the named fixture by the named adapter. Read in full. |
| `partial` | Parsed, but not in full. Lost regions are disclosed and the job lands in `needs_review`. Never a silent success. |
| `rejected` | Refused with a typed error before any parser sees the bytes. |

`ingestion.format_matrix.runtime_support()` reports, per cell, whether the
module behind that cell's proving adapter imports in this deployment. It is a
probe, not a promise: `False` never blocks ingestion, it only means the
stronger wording above must not be used for that cell — which is exactly what
each cell's runtime caveat spells out.

## Regenerating the fixtures

```bash
python3 tests/fixtures/formats/_generate_formats.py
```

Stdlib only — the PDFs are hand-written PDF 1.4, the DOCX/XLSX are `zipfile`
Office Open XML, the PNG is `struct` + `zlib`. No new dependency, and the PD-001
corpus generator (`tests/fixtures/pd001/_generate.py`) is deliberately left
alone so its pinned binaries stay byte-identical.
