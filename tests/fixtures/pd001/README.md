# PD-001 Parser Fixtures

PD-001 fixtures: the **six required** ones (TASK-M0-004 requirement 2; fixture
plan from `decisions/PD-001-v1-parser-stack.md`) **plus four TXT fixtures**
(wired into the manifest during the M2-001a TEST stage — see provenance under
the table). They are **test data only** — nothing here parses anything.
`expected_elements.json` is the expectation side of the contract that
`tests/contracts/test_document_parser_contract.py` asserts.

| File | Format | Covers | Page/section expectations |
|---|---|---|---|
| `pdf_text_multipage.pdf` | PDF text | 3 pages, title + 3 headings, EN/RO/PT body | pages 1/2/3 + section = nearest heading |
| `pdf_table.pdf` | PDF table | 2 pages, ruled 3×4 tables (`pdfplumber.find_tables` detects them) | table cells keep page + section |
| `docx_headings_table.docx` | DOCX | Heading 1/2 styles, RO + PT paragraphs, 4×3 table | section from heading styles (no page map in DOCX) |
| `html_article_nav.html` | HTML | `<nav>`/`<footer>`/`<script>` boilerplate + `<article>` | section from `<h2>`; boilerplate excluded |
| `markdown_structure.md` | Markdown | H1/H2, RO/PT sections, list, fenced code, link | section from `##`; code stays a CODE element |
| `csv_multilingual.csv` | CSV | header, quoted commas, rows in RO/PT/EN | `metadata.language` per row (`ro`/`pt`/`en`) |
| `txt_en.txt` | TXT (UTF-8) | paragraphs, EN | flat text: blank-line paragraphs (no page/section) |
| `txt_ro.txt` | TXT (UTF-8) | paragraphs, full RO diacritics | UTF-8 decode must preserve ș/ț/ă/â/î |
| `txt_pt.txt` | TXT (UTF-8) | paragraphs, PT accents | UTF-8 decode must preserve á/é/ç/ã/õ |
| `txt_cp1252.txt` | TXT (**Windows-1252**) | charset detection — **not** valid UTF-8 | byte `0x83` (glyph `ƒ`) is cp1252-only, so detection is deterministic |

TXT provenance: `txt_en/ro/pt` were created by code1 with the M2-001b
language-tagging work (2026-10-07 06:57) and wired into the manifest by test1
during the M2-001a TEST stage; `txt_cp1252.txt` was added by test1 for the
encoding-detection contract (code2's M2-001a review Minor #3).

### Why `txt_cp1252.txt` carries byte `0x83`

M2-001a test-stage finding: charset-normalizer **ties** short Western texts
between `cp1250` and `cp1252` (coherence 0.6 vs 0.6) and its tie-break picked
**cp1250**, silently mapping `ã → ă` (`acentuação` decoded as `acentuaçăo`).
One byte that Central-European codepages cannot decode (`0x83` = the cp1252
glyph `ƒ`) makes detection deterministic: best = `cp1252`, accents intact.
`test_cp1252_txt_fixture_is_intentionally_not_utf8` pins the encoding so the
fixture can never be quietly re-saved as UTF-8.

## Reading the manifest

```jsonc
{
  "id": "pdf_text_multipage",          // fixture id (unique)
  "file": "pdf_text_multipage.pdf",
  "mime_type": "application/pdf",      // passed to DocumentParser.parse(...)
  "parser_module": "ingestion.adapters.pdf_text",   // where M2 registers the parser
  "page_count": 3,
  "expect_page_preserved": true,       // structure assertion (no parser needed)
  "expect_section_preserved": true,
  "raw_contains": ["2. Metodologie"],  // bytes-level check (fixtures are ASCII/UTF-8 greppable)
  "excluded_text": ["Cookie policy"],  // boilerplate that must not survive normalization
  "expected_elements": [               // matched after a real parse
    {"type": "heading", "text_contains": "2. Metodologie", "page": 2},
    {"type": "text", "text_contains": "Buna ziua", "page": 2, "section_contains": "Metodologie"}
  ]
}
```

Match rule (all fields present must hold): `type` exact, `text_contains`
substring of `DocumentElement.text`, `page` exact, `section_contains` substring
of `DocumentElement.section`, `metadata_contains` equality per key.

## Regenerating

```bash
python3 tests/fixtures/pd001/_generate.py
```

The authoring script is standard-library only (no reportlab/python-docx/pandoc
on this host, and TASK-M0-004 forbids new dependencies): `zipfile` for the DOCX,
plain PDF 1.4 syntax for the PDFs (uncompressed streams so `raw_contains` works).
PDF strings must be cp1252-safe (Helvetica/WinAnsi cannot encode ă/ș/ț — the
script raises if one slips in); the UTF-8 fixtures carry the full RO/PT set.

## Validation evidence (M0-004, outside the repo)

Run with the PD-001 libraries in a throwaway venv (`/root/.venvs/m004-validate`,
not a repo dependency — those become M2 runtime deps):

```text
pypdf 6.19.0        -> pdf_text_multipage: 3 pages, headings/body on the right pages,
                       no cross-page bleed; PT accents intact
pdfplumber 0.11.10  -> pdf_table: find_tables() = 1 table per page, 'Revenue' and
                       '1450' cells extracted
python-docx         -> headings [(Heading 1), (Heading 2) x2], RO/PT diacritics,
                       4x3 table with header row, body after table
stdlib              -> HTML/MD/CSV structure, CSV lang tags ro/pt/en/ro, docx OOXML
                       parts well-formed, manifest = 6 fixtures
FIXTURE_VALIDATION_OK
```

## Known limitations

- The PDF fixtures use WinAnsi-safe text; a diacritic-heavy RO PDF needs a
  Unicode font path (M2 decision, revisit with the parser adapter).
- ~~`TXT` (PD-001 format family) is not part of the required six~~ — **resolved
  in the M2-001a TEST stage**: four TXT fixtures are present and manifest-wired
  (`txt_en/ro/pt` UTF-8 trio + `txt_cp1252` encoding variant); the six
  *required* ids in TASK-M0-004 are unchanged.
- Charset detection for non-UTF-8 `text/plain` is heuristic (see the `0x83`
  note above): M2-001c ingestion should consider a charset hint from connector
  metadata when a source declares one, rather than trusting detection alone.
