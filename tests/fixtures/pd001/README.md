# PD-001 Parser Fixtures

Six fixtures, one per required source type (`tasks/M0/TASK-M0-004.md` requirement 2;
fixture plan from `decisions/PD-001-v1-parser-stack.md`). They are **test data
only** — nothing here parses anything. `expected_elements.json` is the
expectation side of the contract that `tests/contracts/test_document_parser_contract.py`
asserts.

| File | Format | Covers | Page/section expectations |
|---|---|---|---|
| `pdf_text_multipage.pdf` | PDF text | 3 pages, title + 3 headings, EN/RO/PT body | pages 1/2/3 + section = nearest heading |
| `pdf_table.pdf` | PDF table | 2 pages, ruled 3×4 tables (`pdfplumber.find_tables` detects them) | table cells keep page + section |
| `docx_headings_table.docx` | DOCX | Heading 1/2 styles, RO + PT paragraphs, 4×3 table | section from heading styles (no page map in DOCX) |
| `html_article_nav.html` | HTML | `<nav>`/`<footer>`/`<script>` boilerplate + `<article>` | section from `<h2>`; boilerplate excluded |
| `markdown_structure.md` | Markdown | H1/H2, RO/PT sections, list, fenced code, link | section from `##`; code stays a CODE element |
| `csv_multilingual.csv` | CSV | header, quoted commas, rows in RO/PT/EN | `metadata.language` per row (`ro`/`pt`/`en`) |

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
- `TXT` (PD-001 format family) is not part of the required six; add it with a
  manifest entry when M2 lands the text parser.
