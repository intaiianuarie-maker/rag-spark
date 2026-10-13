"""Author the six T-V1-031 format-matrix fixtures (test data — no parsing logic).

Run from the repo root:

    python3 tests/fixtures/formats/_generate_formats.py

Why a second generator (reuse check)
------------------------------------
``tests/fixtures/pd001/_generate.py`` already authors the PD-001 parser corpus
with the standard library alone and its binaries are pinned byte-for-byte by
the M0-004 contract test — re-running it with new payloads would rewrite those
fixtures and break pins that are not this ticket's to touch. The format matrix
needs three fixtures that corpus does not have (an **image-only** PDF, a
**table-heavy** PDF, an **XLSX**) plus its own digital-PDF/DOCX/image cells, so
they live here, in their own directory, generated the same stdlib-only way.

No new dependency is introduced: PDF 1.4 syntax and Office Open XML are written
by hand, the XLSX is a `zipfile` of the six parts Excel requires, and the PNG is
`struct` + `zlib` (a single IDAT). The generated files are checked for validity
by the test suite that consumes them (`tests/unit/test_format_matrix.py`).

Text inside the PDFs is kept cp1252-safe because Helvetica/WinAnsiEncoding
cannot encode ă/ș/ț — the generator raises if a non-WinAnsi character reaches a
PDF string.
"""

from __future__ import annotations

import struct
import zlib
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
    return b"BT /%s %d Tf %g %g Td (%s) Tj ET" % (font.encode(), size, x, y, _esc(text))


def line_op(x1: float, y1: float, x2: float, y2: float) -> bytes:
    return b"%g %g m %g %g l S" % (x1, y1, x2, y2)


def heading_ops(y: float, text: str) -> list[bytes]:
    return [text_op(72, y, 15, "F2", text)]


def paragraph_ops(y_start: float, lines: list[str], size: int = 11) -> list[bytes]:
    return [text_op(72, y_start - index * 16, size, "F1", line) for index, line in enumerate(lines)]


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


def build_pdf(pages: list[dict]) -> bytes:
    """Assemble page specs into a single-file PDF.

    Each spec is ``{"ops": [...], "image": bool}``. ``image`` pages get an
    ``/XObject`` image and **no** text operators — a scanned page.
    """
    page_ids = [5 + 2 * index for index in range(len(pages))]
    content_ids = [6 + 2 * index for index in range(len(pages))]
    image_id = 5 + 2 * len(pages)

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

    needs_image = any(spec.get("image") for spec in pages)
    if needs_image:
        raw = b"".join(
            bytes((192 - (row * 12) % 64, 60 + (col * 8) % 96, 30)) * 4
            for row in range(24)
            for col in range(24)
        )
        objects[image_id] = (
            b"<< /Type /XObject /Subtype /Image /Width 96 /Height 24 "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            b"/Length %d >>\nstream\n" % len(zlib.compress(raw))
            + zlib.compress(raw)
            + b"\nendstream"
        )

    for index, spec in enumerate(pages):
        page_id, content_id = page_ids[index], content_ids[index]
        if spec.get("image"):
            resources = (
                b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> /XObject << "
                + f"/Im0 {image_id} 0 R >> >>".encode()
            )
            stream = b"q %g 0 0 %g 0 0 cm /Im0 Do Q" % (PAGE_WIDTH, PAGE_HEIGHT)
        else:
            resources = b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >>"
            stream = b"\n".join(spec["ops"])
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] ".encode()
            + resources
            + f" /Contents {content_id} 0 R >>".encode()
        )
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


# --------------------------------------------------------------------------------------
# The six matrix fixtures
# --------------------------------------------------------------------------------------


def pdf_digital() -> bytes:
    """Two pages of real text: the "digital PDF" cell (text layer present)."""
    page_1 = [
        *heading_ops(720, "Format Matrix Digital PDF Fixture"),
        *paragraph_ops(
            690,
            [
                "1. Introduction",
                "This fixture proves the digital PDF cell: the text layer is",
                "read in full, with the page number preserved on every element.",
            ],
        ),
    ]
    page_2 = [
        *heading_ops(720, "2. Concluzii"),
        *paragraph_ops(
            690,
            [
                "Section tracking keeps each paragraph attached to its heading.",
                "Olá, este é um documento de teste para a matriz de formatos.",
            ],
        ),
    ]
    return build_pdf([{"ops": page_1}, {"ops": page_2}])


def pdf_tables_heavy() -> bytes:
    """Three pages of wide ruled tables: the "table-heavy PDF" cell."""
    header = ["Region", "Product", "Units", "Revenue", "Cost", "Margin"]
    pages = []
    for page_index in range(3):
        rows = [header]
        for row_index in range(11):
            rows.append(
                [
                    f"R{page_index}{row_index}",
                    f"SKU-{page_index}{row_index:02d}",
                    str(100 + row_index * 7),
                    str(1200 + row_index * 130),
                    str(800 + row_index * 90),
                    str(400 + row_index * 40),
                ]
            )
        pages.append(
            {
                "ops": [
                    *heading_ops(740, f"Quarterly Ledger {page_index + 1}"),
                    *paragraph_ops(
                        720,
                        [
                            f"Ledger body paragraph for period {page_index + 1} that is "
                            "not inside any table.",
                            "It must survive as TEXT next to the ruled table below.",
                        ],
                    ),
                    table_ops(40, 660, [70, 130, 60, 90, 90, 80], rows),
                ]
            }
        )
    return build_pdf(pages)


def pdf_scanned() -> bytes:
    """Three image-only pages: the "scanned PDF" cell (no text layer at all)."""
    return build_pdf([{"ops": [], "image": True} for _ in range(3)])


def pdf_mixed_scan() -> bytes:
    """One readable page + one image-only page: the *partially* lost case."""
    readable = [
        *heading_ops(720, "Partially Scanned Report"),
        *paragraph_ops(
            690,
            [
                "1. Summary",
                "Page one carries a text layer and must be read normally.",
            ],
        ),
    ]
    return build_pdf([{"ops": readable}, {"ops": [], "image": True}])


# --------------------------------------------------------------------------------------
# DOCX (minimal Office Open XML package)
# --------------------------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _paragraph(style: str, text: str) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{style_xml}<w:r><w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r></w:p>'


def _table(rows: list[list[str]]) -> str:
    cells = []
    for row in rows:
        cells.append(
            "<w:tr>"
            + "".join(
                '<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
                + _paragraph("", cell)
                + "</w:tc>"
                for cell in row
            )
            + "</w:tr>"
        )
    return '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>' + "".join(cells) + "</w:tbl>"


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
CT_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"


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
    document = f"""{XML_HEADER}
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


def docx_tables() -> bytes:
    """Headings + a wide table: the DOCX cell."""
    header = ["Region", "Product", "Units", "Revenue", "Cost", "Margin"]
    rows = [header] + [
        [
            f"R{i}",
            f"SKU-{i:02d}",
            str(100 + i * 7),
            str(1200 + i * 130),
            str(800 + i * 90),
            str(400 + i * 40),
        ]
        for i in range(12)
    ]
    body = "".join(
        [
            _paragraph("Heading1", "Format Matrix DOCX Fixture"),
            _paragraph("", "Introduction paragraph read in full by the DOCX adapter."),
            _paragraph("Heading2", "Monthly ledger"),
            _table(rows),
            _paragraph("", "Closing paragraph after the table."),
        ]
    )
    return build_docx(body)


# --------------------------------------------------------------------------------------
# XLSX (the rejected cell: a real workbook, so the refusal is a refusal of *content*)
# --------------------------------------------------------------------------------------

XLSX_CT = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
XLSX_SHEET_CT = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
)
XLSX_WORKBOOK_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
)


def xlsx_workbook() -> bytes:
    """A minimal but valid XLSX — the point is that it is refused, not corrupted."""
    rows = [
        ["Region", "Product", "Units", "Revenue"],
        ["R0", "SKU-00", "100", "1200"],
        ["R1", "SKU-01", "107", "1330"],
        ["R2", "SKU-02", "114", "1460"],
    ]
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{_col_letter(col_index)}{row_index}" t="inlineStr">'
            f"<is><t>{_xml_escape(cell)}</t></is></c>"
            for col_index, cell in enumerate(row, start=1)
        )
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')
    sheet = (
        XML_HEADER
        + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook = (
        XML_HEADER
        + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        + 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        + '<sheets><sheet name="Ledger" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        XML_HEADER
        + f'<Relationships xmlns="{REL_NS}">'
        + f'<Relationship Id="rId1" Type="{XLSX_WORKBOOK_REL}" Target="worksheets/sheet1.xml"/>'
        + "</Relationships>"
    )
    root_rels = (
        XML_HEADER
        + f'<Relationships xmlns="{REL_NS}">'
        + f'<Relationship Id="rId1" Type="{OFFICE_DOC_REL}" Target="xl/workbook.xml"/>'
        + "</Relationships>"
    )
    content_types = "\n".join(
        [
            XML_HEADER,
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            f' <Default Extension="rels" ContentType="{CT_RELS}"/>',
            ' <Default Extension="xml" ContentType="application/xml"/>',
            f' <Override PartName="/xl/workbook.xml" ContentType="{XLSX_CT}"/>',
            f' <Override PartName="/xl/worksheets/sheet1.xml" ContentType="{XLSX_SHEET_CT}"/>',
            "</Types>",
        ]
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, content in (
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", root_rels),
            ("xl/workbook.xml", workbook),
            ("xl/_rels/workbook.xml.rels", workbook_rels),
            ("xl/worksheets/sheet1.xml", sheet),
        ):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()


def _col_letter(index: int) -> str:
    return chr(ord("A") + index - 1)


# --------------------------------------------------------------------------------------
# PNG (stdlib: signature + IHDR + IDAT + IEND)
# --------------------------------------------------------------------------------------


def png_image(width: int = 32, height: int = 32) -> bytes:
    """A valid RGB PNG — the image cell fixture (stdlib only)."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # One filter byte per scanline, then packed RGB triplets.
    rows = b"".join(
        b"\x00" + bytes(byte for col in range(width) for byte in (row % 256, (col * 7) % 256, 120))
        for row in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------

PAYLOADS: tuple[tuple[str, bytes], ...] = (
    ("pdf_digital.pdf", pdf_digital()),
    ("pdf_tables_heavy.pdf", pdf_tables_heavy()),
    ("pdf_scanned.pdf", pdf_scanned()),
    ("pdf_mixed_scan.pdf", pdf_mixed_scan()),
    ("docx_tables.docx", docx_tables()),
    ("sheet.xlsx", xlsx_workbook()),
    ("scan.png", png_image()),
)


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, int]] = []
    for name, payload in PAYLOADS:
        path = HERE / name
        path.write_bytes(payload)
        written.append((name, path.stat().st_size))
    for name, size in written:
        print(f"wrote {name:24s} {size:6d} bytes")


if __name__ == "__main__":
    main()
