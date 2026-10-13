"""The honest format matrix (T-V1-031) — code-side single source of truth.

``docs/ingestion/FORMAT-MATRIX.md`` mirrors this table and
``tests/unit/test_format_matrix.py`` fails if the two ever drift, so a cell
cannot be advertised in the docs without being proven (or explicitly rejected)
here.

Rules this table exists to enforce:

1. **Every cell is either PROVEN or EXPLICITLY REJECTED.** There is no third
   state, and in particular no "we advertise it, we just never tested it".
2. **A cell names the adapter that proves it.** "Supported" is a claim about a
   specific adapter on a specific fixture, not about a mime type in general.
3. **A degraded cell carries its caveat with it** (``runtime_caveat``), so a
   deployment that cannot honour the claim says so instead of shipping the
   stronger wording.
4. **A rejected cell states the exact refusal** the uploader will observe, so
   "unsupported" is a typed, testable answer rather than a silent no-op.

The matrix covers exactly the six formats the ticket names: digital PDF,
table-heavy PDF, scanned PDF, DOCX, XLSX and an image.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Fixture directory holding the matrix's six fixtures (authored by
#: ``tests/fixtures/formats/_generate_formats.py``).
FIXTURE_DIR = "tests/fixtures/formats"

DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
DOCX_MAIN_MIME = f"{DOCX_MIME}.main+xml"
PDF_MIME = "application/pdf"


class FormatOutcome(StrEnum):
    """What one matrix cell honestly claims about one format."""

    SUPPORTED = "supported"
    #: Parsed, but not in full: the lost regions are disclosed and the job
    #: lands in ``needs_review``. Never a silent success.
    PARTIAL = "partial"
    #: Refused with a typed error before any parser sees the bytes.
    REJECTED = "rejected"


@dataclass(frozen=True)
class FormatCell:
    """One advertised format cell."""

    key: str
    label: str
    outcome: FormatOutcome
    #: Mime(s) this cell is about.
    mimes: tuple[str, ...]
    #: Fixture proving the cell (relative to the repo root); ``None`` for a
    #: rejected cell, whose proof is the typed refusal of *any* bytes.
    fixture: str | None
    #: The adapter whose behaviour is the proof (class name in
    #: ``packages/ingestion/adapters``), or the detector that detects it.
    proven_by: str
    #: Importable module holding ``proven_by`` — the claim's precondition.
    proven_by_module: str
    #: What this cell guarantees, worded exactly as the docs page words it.
    claim: str
    #: What the user observes when the cell is not a clean success.
    disclosure: str
    #: Only for SUPPORTED/PARTIAL cells whose claim depends on an optional
    #: component: what happens in a deployment without it.
    runtime_caveat: str | None = None
    #: Only for REJECTED cells: the exact typed refusal.
    rejection: str | None = None


#: The matrix, in the order the docs page renders it.
FORMAT_MATRIX: tuple[FormatCell, ...] = (
    FormatCell(
        key="pdf_digital",
        label="Digital PDF (has a text layer)",
        outcome=FormatOutcome.SUPPORTED,
        mimes=(PDF_MIME,),
        fixture=f"{FIXTURE_DIR}/pdf_digital.pdf",
        proven_by="PdfTextParser",
        proven_by_module="ingestion.adapters.pdf_text",
        claim=(
            "Text is extracted per page with the page number preserved, "
            "numbered headings become sections, and the first line becomes the "
            "title element."
        ),
        disclosure="None: the whole document is read.",
        runtime_caveat=(
            "Docling is the PRIMARY PDF parser (T-V1-030). A deployment without "
            "docling falls back to pypdf for this cell and the fallback is "
            "disclosed on every chunk (`parse.fallback_used`) and on the job."
        ),
    ),
    FormatCell(
        key="pdf_tables",
        label="Table-heavy PDF",
        outcome=FormatOutcome.SUPPORTED,
        mimes=(PDF_MIME,),
        fixture=f"{FIXTURE_DIR}/pdf_tables_heavy.pdf",
        proven_by="PdfTableParser",
        proven_by_module="ingestion.adapters.pdf_table",
        claim=(
            "Ruled tables survive as TABLE elements with their rows intact — "
            "never flattened into a text soup — and body text outside a table "
            "stays TEXT."
        ),
        disclosure="None while the table adapter runs.",
        runtime_caveat=(
            "The claim is a claim about the TABLE adapter. Docling (primary, "
            "T-V1-030) preserves table content; pypdf (the text fallback) "
            "returns table rows as plain lines and loses the TABLE structure. "
            "A deployment without docling therefore reads this cell as text — "
            "disclosed via `parse.fallback_used`, never presented as a table "
            "extraction."
        ),
    ),
    FormatCell(
        key="pdf_scanned",
        label="Scanned PDF (image-only, no text layer)",
        outcome=FormatOutcome.PARTIAL,
        mimes=(PDF_MIME,),
        fixture=f"{FIXTURE_DIR}/pdf_scanned.pdf",
        proven_by="pdf_text_layer_report",
        proven_by_module="ingestion.adapters.pdf_scan",
        claim=(
            "An image-only page is never read as if it were read: no text is "
            "invented, the lost pages are listed, and the job ends in "
            "`needs_review` rather than `ready`."
        ),
        disclosure=(
            "Fully image-only document: `needs_review` with the lost page list "
            "and zero chunks. Partially image-only: the readable text is "
            "published, the lost pages are disclosed on every chunk and the job "
            "still ends `needs_review`."
        ),
        runtime_caveat=(
            "OCR is NOT part of this deployment, so this cell cannot be "
            "upgraded to `supported` by configuration. Adding OCR is a new "
            "decision row, not a config change."
        ),
    ),
    FormatCell(
        key="docx",
        label="DOCX (Word)",
        outcome=FormatOutcome.SUPPORTED,
        mimes=(DOCX_MIME, DOCX_MAIN_MIME),
        fixture=f"{FIXTURE_DIR}/docx_tables.docx",
        proven_by="DocxParser",
        proven_by_module="ingestion.adapters.docx",
        claim=(
            "Body order is preserved, Title/Heading 1 becomes TITLE, lower "
            "headings become sections, and tables stay TABLE elements."
        ),
        disclosure="None: the whole document is read.",
        runtime_caveat=(
            "Docling is the PRIMARY DOCX parser (T-V1-030); without docling the "
            "cell is served by python-docx and the fallback is disclosed."
        ),
    ),
    FormatCell(
        key="xlsx",
        label="XLSX (Excel)",
        outcome=FormatOutcome.REJECTED,
        mimes=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        ),
        fixture=f"{FIXTURE_DIR}/sheet.xlsx",
        proven_by="parser_for_mime",
        proven_by_module="ingestion.adapters.registry",
        claim=(
            "There is no XLSX adapter in v1. An upload is refused with a typed "
            "`unsupported_mime_type` BEFORE any parser sees the bytes — it is "
            "never half-ingested and never silently dropped."
        ),
        disclosure=(
            "The upload/API answers `unsupported_mime_type`; the job (if "
            "created) fails non-retryably with the same code."
        ),
        rejection=(
            "IngestPipelineError('unsupported_mime_type') — no parser is "
            "registered for the mime, so nothing is parsed, stored or indexed."
        ),
    ),
    FormatCell(
        key="image",
        label="Image (PNG / JPEG)",
        outcome=FormatOutcome.REJECTED,
        mimes=("image/png", "image/jpeg"),
        fixture=f"{FIXTURE_DIR}/scan.png",
        proven_by="parser_for_mime",
        proven_by_module="ingestion.adapters.registry",
        claim=(
            "Images are not a v1 document format. An upload is refused with a "
            "typed `unsupported_mime_type` BEFORE any parser sees the bytes — "
            "an image is never silently indexed as an empty document."
        ),
        disclosure="The upload/API answers `unsupported_mime_type`.",
        rejection=(
            "IngestPipelineError('unsupported_mime_type') — no parser is "
            "registered for the mime, so nothing is parsed, stored or indexed."
        ),
    ),
)

#: Convenience indexes.
MATRIX_BY_KEY: dict[str, FormatCell] = {cell.key: cell for cell in FORMAT_MATRIX}
MATRIX_KEYS: tuple[str, ...] = tuple(cell.key for cell in FORMAT_MATRIX)

#: Every mime the matrix explicitly refuses (XLSX + images). The registry
#: already refuses them — this list is what makes the refusal *advertised and
#: pinned* rather than incidental.
REJECTED_MIMES: frozenset[str] = frozenset(
    mime for cell in FORMAT_MATRIX if cell.outcome is FormatOutcome.REJECTED for mime in cell.mimes
)


def runtime_support() -> dict[str, bool]:
    """Can this deployment honour each cell's proving adapter?

    A probe, not a promise: it reports whether the module behind the cell's
    proving adapter imports here. ``False`` never blocks ingestion — it only
    means the docs page's stronger wording must not be used for that cell,
    which is exactly what ``runtime_caveat`` spells out.
    """
    import importlib

    support: dict[str, bool] = {}
    for cell in FORMAT_MATRIX:
        try:
            importlib.import_module(cell.proven_by_module)
        except Exception:
            support[cell.key] = False
        else:
            support[cell.key] = True
    return support


def matrix_outcomes() -> dict[str, str]:
    """``key -> outcome`` for the docs-matches-code pin."""
    return {cell.key: cell.outcome.value for cell in FORMAT_MATRIX}


def cell_for_mime(mime_type: str | None) -> FormatCell | None:
    """The matrix cell a mime belongs to, if the matrix advertises it."""
    if not mime_type:
        return None
    for cell in FORMAT_MATRIX:
        if mime_type in cell.mimes:
            return cell
    return None
