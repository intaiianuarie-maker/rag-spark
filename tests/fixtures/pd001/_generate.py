"""Author the six PD-001 parser fixtures (test data only — no parsing logic).

Run from the repo root:

    python3 tests/fixtures/pd001/_generate.py

Why this script exists (reuse check):
TASK-M0-004 requires six PD-001 fixtures (PDF-text, PDF-table, DOCX, HTML, MD,
CSV). Four of them are plain text and are committed directly. The two binary
fixtures need an *authoring* tool, and this host has none (no reportlab, no
python-docx, no pandoc, no libreoffice), while TASK-M0-004 forbids new
dependencies — so the binaries are emitted with the standard library only
(`zipfile` for DOCX, plain PDF 1.4 syntax for the PDFs). This is fixture
authoring, not parsing: it writes source documents, it never reads them.

Validity was checked ad-hoc with pypdf (PDF text/page extraction) and
python-docx (DOCX open) outside the repo — those are M2 runtime dependencies
per `decisions/PD-001-v1-parser-stack.md`, not test dependencies. See
`handoffs/M0-004-test1.md` for the recorded validation run.

Text is kept cp1252-safe in the PDFs (Helvetica/WinAnsiEncoding cannot encode
ă/ș/ț — those characters live in the UTF-8 fixtures MD/HTML/CSV/DOCX); the
generator raises if a non-WinAnsi character reaches a PDF string.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

HERE = Path(__file__).resolve().parent

PAGE_WIDTH = 612
PAGE_HEIGHT = 792


# --------------------------------------------------------------------------------------
# PDF authoring (minimal PDF 1.4, uncompressed streams so fixtures stay greppable)
# --------------------------------------------------------------------------------------


def _esc(text: str) -> bytes:
    try:
        raw = text.encode("cp1252")
    except UnicodeEncodeError as exc:  # pragma: no cover - authoring guard
        raise ValueError(
            f"PDF fixture text must be cp1252-safe (Helvetica/WinAnsi): {text!r}"
        ) from exc
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def text_op(x: float, y: float, size: int, font: str, text: str) -> bytes:
    return b"BT /%s %d Tf %g %g Td (%s) Tj ET" % (
        font.encode(),
        size,
        x,
        y,
        _esc(text),
    )


def line_op(x1: float, y1: float, x2: float, y2: float) -> bytes:
    return b"%g %g m %g %g l S" % (x1, y1, x2, y2)


def table_ops(x: float, y_top: float, widths: list[float], rows: list[list[str]]) -> bytes:
    """Draw a ruled table: horizontal + vertical rules with text in each cell."""
    row_height = 22.0
    total_width = sum(widths)
    ops: list[bytes] = [b"0.7 w"]
    for row_index in range(len(rows) + 1):
        y = y_top - row_index * row_height
        ops.append(line_op(x, y, x + total_width, y))
    edge = x
    for width in widths:
        ops.append(line_op(edge, y_top, edge, y_top - len(rows) * row_height))
        edge += width
    ops.append(line_op(edge, y_top, edge, y_top - len(rows) * row_height))
    for row_index, row in enumerate(rows):
        cell_x = x
        for col_index, cell in enumerate(row):
            baseline = y_top - (row_index + 1) * row_height + 6
            font = "F2" if row_index == 0 else "F1"
            ops.append(text_op(cell_x + 4, baseline, 9, font, cell))
            cell_x += widths[col_index]
    return b"\n".join(ops)


def heading_ops(y: float, text: str) -> list[bytes]:
    return [text_op(72, y, 15, "F2", text)]


def paragraph_ops(y_start: float, lines: list[str], size: int = 11) -> list[bytes]:
    return [
        text_op(72, y_start - index * 16, size, "F1", line)
        for index, line in enumerate(lines)
    ]


def build_pdf(pages: list[list[bytes]]) -> bytes:
    """Assemble page content streams into a valid single-file PDF."""
    page_ids = [5 + 2 * index for index in range(len(pages))]
    content_ids = [6 + 2 * index for index in range(len(pages))]

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids ["
        + b" ".join(f"{pid} 0 R".encode() for pid in page_ids)
        + f"] /Count {len(pages)} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>",
    }
    for index, ops in enumerate(pages):
        page_id, content_id = page_ids[index], content_ids[index]
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode()
        stream = b"\n".join(ops)
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"

    startxref = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for number in sorted(objects):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def pdf_text_multipage() -> bytes:
    """Three pages: title, three headings, EN/RO/PT body text (page map matters)."""
    page_1 = [
        *heading_ops(720, "Knowledge OS Parser Fixture"),
        *paragraph_ops(
            690,
            [
                "1. Introduction",
                "This document exercises page preservation for hybrid retrieval",
                "and citations in the Knowledge OS ingestion pipeline.",
            ],
        ),
    ]
    page_2 = [
        *heading_ops(720, "2. Metodologie"),
        *paragraph_ops(
            690,
            [
                "Buna ziua, acesta este un document de testare pentru pagini.",
                "Olá, este é um documento de teste para páginas.",
                "Section tracking keeps each paragraph attached to its heading.",
            ],
        ),
    ]
    page_3 = [
        *heading_ops(720, "3. Concluzii"),
        *paragraph_ops(
            690,
            [
                "Conclusion paragraph on page three with section tracking.",
                "Every element below must keep its page number after parsing.",
            ],
        ),
    ]
    return build_pdf([page_1, page_2, page_3])


def pdf_table() -> bytes:
    """Two pages, each a ruled 3x4 table (tables must not flatten to text soup)."""
    page_1 = [
        *heading_ops(740, "Quarterly Numbers"),
        table_ops(
            72,
            700,
            [220, 110, 110],
            [
                ["Metric", "Q1", "Q2"],
                ["Revenue", "1200", "1450"],
                ["Costs", "800", "910"],
                ["Margin", "400", "540"],
            ],
        ),
    ]
    page_2 = [
        *heading_ops(740, "Half-year Notes"),
        table_ops(
            72,
            700,
            [220, 110, 110],
            [
                ["Metric", "Q3", "Q4"],
                ["Revenue", "1510", "1625"],
                ["Costs", "930", "975"],
                ["Margin", "580", "650"],
            ],
        ),
    ]
    return build_pdf([page_1, page_2])


# --------------------------------------------------------------------------------------
# DOCX authoring (minimal Office Open XML package)
# --------------------------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _paragraph(style: str, text: str) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{style_xml}<w:r><w:t xml:space=\"preserve\">{_xml_escape(text)}</w:t></w:r></w:p>"


def _table(rows: list[list[str]]) -> str:
    cells = []
    for row in rows:
        cells.append(
            "<w:tr>"
            + "".join(
                "<w:tc><w:tcPr><w:tcW w:w=\"0\" w:type=\"auto\"/></w:tcPr>"
                + _paragraph("", cell)
                + "</w:tc>"
                for cell in row
            )
            + "</w:tr>"
        )
    return (
        '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>'
        + "".join(cells)
        + "</w:tbl>"
    )


XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_DOC_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
STYLES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
CT_DOCUMENT = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
CT_STYLES = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
CT_RELS = "application/vnd.openxmlformats-package.relationships+xml"


def build_docx(body_xml: str) -> bytes:
    content_types = "\n".join(
        [
            XML_HEADER,
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            f' <Default Extension="rels" ContentType="{CT_RELS}"/>',
            ' <Default Extension="xml" ContentType="application/xml"/>',
            f' <Override PartName="/word/document.xml" ContentType="{CT_DOCUMENT}"/>',
            f' <Override PartName="/word/styles.xml" ContentType="{CT_STYLES}"/>',
            "</Types>",
        ]
    )
    root_rels = "\n".join(
        [
            XML_HEADER,
            f'<Relationships xmlns="{REL_NS}">',
            f' <Relationship Id="rId1" Type="{OFFICE_DOC_REL}" Target="word/document.xml"/>',
            "</Relationships>",
        ]
    )
    doc_rels = "\n".join(
        [
            XML_HEADER,
            f'<Relationships xmlns="{REL_NS}">',
            f' <Relationship Id="rId1" Type="{STYLES_REL}" Target="styles.xml"/>',
            "</Relationships>",
        ]
    )
    styles = "\n".join(
        [
            XML_HEADER,
            f'<w:styles xmlns:w="{W_NS}">',
            ' <w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
            '<w:name w:val="Normal"/></w:style>',
            ' <w:style w:type="paragraph" w:styleId="Heading1">'
            '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/></w:style>',
            ' <w:style w:type="paragraph" w:styleId="Heading2">'
            '<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/></w:style>',
            "</w:styles>",
        ]
    )
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}"><w:body>{body_xml}<w:sectPr/></w:body></w:document>
"""

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, content in (
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", root_rels),
            ("word/_rels/document.xml.rels", doc_rels),
            ("word/styles.xml", styles),
            ("word/document.xml", document),
        ):
            # Fixed mtime keeps regeneration byte-identical (zip stores timestamps).
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()


def docx_headings_table() -> bytes:
    body = "".join(
        [
            _paragraph("Heading1", "Knowledge OS DOCX Fixture"),
            _paragraph("", "Introduction paragraph with hybrid retrieval and citations."),
            _paragraph("Heading2", "Date lunare"),
            _paragraph("", "Acesta este un paragraf în limba română despre căutare."),
            _paragraph("Heading2", "Secção mensal"),
            _paragraph("", "Este é um parágrafo em português sobre recuperação."),
            _table(
                [
                    ["Metric", "Q1", "Q2"],
                    ["Revenue", "1200", "1450"],
                    ["Costs", "800", "910"],
                    ["Margin", "400", "540"],
                ]
            ),
            _paragraph("", "Closing paragraph after the table."),
        ]
    )
    return build_docx(body)


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------

TEXT_FIXTURES = {
    "html_article_nav.html": """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Knowledge OS HTML Fixture</title>
</head>
<body>
  <nav aria-label="Primary">
    <a href="/">Home</a>
    <a href="/pricing">Pricing</a>
    <a href="/cookies">Cookie policy</a>
  </nav>
  <article>
    <h1>Knowledge OS HTML Fixture</h1>
    <p>This article body must survive boilerplate stripping for hybrid retrieval.</p>
    <h2>Secțiunea în română</h2>
    <p>Acesta este un paragraf în limba română despre documente indexate.</p>
    <h2>Secção em português</h2>
    <p>Este é um parágrafo em português sobre documentos indexados.</p>
    <ul>
      <li>primul element din listă</li>
      <li>al doilea element din listă</li>
    </ul>
  </article>
  <footer>
    <p>Footer boilerplate — Copyright 2026, all rights reserved.</p>
  </footer>
  <script>window.analyticsQueue = ["pageview"];</script>
</body>
</html>
""",
    "markdown_structure.md": """# Knowledge OS Markdown Fixture

Introduction paragraph mentioning hybrid retrieval and citations (EN).

## Secțiunea în română

Acesta este un paragraf în limba română despre instrumente de căutare.

## Secção em português

Este é um parágrafo em português sobre recuperação de informação.

- primul element din listă
- al doilea element din listă

```python
def fuse(rankings: list[list[str]]) -> dict[str, float]:
    # code must stay a CODE element, never flatten into text soup
    return {}
```

[Trailing paragraph with a link](https://example.invalid/doc) after the code block.
""",
    "csv_multilingual.csv": """id,title,body,lang
1,"Raport lunar, cu virgule","Bună ziua, acesta este un raport despre căutare hibridă.",ro
2,"Relatório mensal, com vírgulas","Olá, este é um relatório sobre recuperação de informação.",pt
3,"Monthly report, with commas","Hello, this is a monthly report about hybrid search.",en
4,"Câmp cu ghilimele","Ana are, mere, și pere — celule cu virgule intern.",ro
""",
}


def main() -> None:
    payloads: list[tuple[str, bytes]] = [
        ("pdf_text_multipage.pdf", pdf_text_multipage()),
        ("pdf_table.pdf", pdf_table()),
        ("docx_headings_table.docx", docx_headings_table()),
        *[(name, text.encode("utf-8")) for name, text in TEXT_FIXTURES.items()],
    ]
    written = []
    for name, payload in payloads:
        path = HERE / name
        path.write_bytes(payload)
        written.append((name, path.stat().st_size))

    for name, size in written:
        print(f"wrote {name:28s} {size} bytes")


if __name__ == "__main__":
    main()
