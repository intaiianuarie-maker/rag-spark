"""T-V1-031 — the honest format matrix, proven cell by cell.

Three things are pinned here, in the order the ticket states them:

1. **Every advertised cell is proven or explicitly rejected** (6/6). The cells
   live in ``ingestion.format_matrix`` and the docs page
   (``docs/ingestion/FORMAT-MATRIX.md``) is checked *against the code*, so a
   doc row cannot advertise what the code does not do.
2. **Budgets are enforced pre-expansion** — the tests assert the element count
   *at the moment of the breach* (budget + 1), which is only possible if the
   guard trips between elements rather than counting them afterwards.
3. **Partial is disclosed and never confident** — a partially-read source parks
   the job ``needs_review``, carries its lost regions onto every chunk, and the
   answer layer refuses to render an answer over it as ``answered``/``HIGH``.

The PD-001 corpus keeps its own contract test (``tests/contracts/
test_document_parser_contract.py``); this file is the matrix's.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from core.errors import (
    JOB_STATUS_REASON_CODES,
    AnswerStatus,
    JobStatus,
    ReasonCode,
)
from harness_fakes import make_item
from ingestion.adapters.docx import DocxParser
from ingestion.adapters.pdf_scan import pdf_text_layer_report
from ingestion.adapters.pdf_table import PdfTableParser
from ingestion.adapters.pdf_text import PdfTextParser
from ingestion.adapters.registry import (
    ParseAttempt,
    ParseReport,
    parse_with_precedence,
    parser_chain_for,
    parser_for_mime,
)
from ingestion.budget import (
    BUDGET_CODES,
    EXPANDED_TEXT_BUDGET,
    OUTPUT_BUDGET,
    ParseBudgetExceeded,
    ParseBudgetGuard,
    ParseBudgets,
    parse_with_budgets,
)
from ingestion.format_matrix import (
    FORMAT_MATRIX as MATRIX,
)
from ingestion.format_matrix import (
    MATRIX_BY_KEY,
    MATRIX_KEYS,
    REJECTED_MIMES,
    FormatOutcome,
    cell_for_mime,
    matrix_outcomes,
    runtime_support,
)
from ingestion.interfaces import DocumentParser, ElementType
from ingestion.normalize import make_element
from jobs.ingest_pipeline import terminal_job_state

pytestmark = pytest.mark.unit

PDF_MIME = "application/pdf"
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PNG_MIME = "image/png"

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "formats"
DOCS_PAGE = REPO_ROOT / "docs" / "ingestion" / "FORMAT-MATRIX.md"


def _fixture(name: str) -> bytes:
    return (MATRIX_FIXTURES / name).read_bytes()


# --------------------------------------------------------------------------------------
# 1. The matrix itself: 6 cells, every one proven or explicitly rejected
# --------------------------------------------------------------------------------------


def test_matrix_covers_exactly_the_six_ticket_formats() -> None:
    assert MATRIX_KEYS == ("pdf_digital", "pdf_tables", "pdf_scanned", "docx", "xlsx", "image")


def test_every_cell_is_supported_partial_or_rejected_never_something_else() -> None:
    allowed = {member.value for member in FormatOutcome}
    assert set(matrix_outcomes().values()) <= allowed
    # No cell may sit unlabelled: the ticket's metric is 6/6 explicit.
    assert len(matrix_outcomes()) == 6


def test_every_cell_names_an_existing_fixture_and_an_importable_prover() -> None:
    import importlib

    for cell in MATRIX:
        assert cell.fixture is not None, f"{cell.key}: every cell needs its fixture"
        path = REPO_ROOT / cell.fixture
        assert path.is_file(), f"{cell.key}: missing fixture {path}"
        module = importlib.import_module(cell.proven_by_module)
        if cell.outcome is FormatOutcome.REJECTED:
            # A rejected cell is proven by the ABSENCE of a parser for the mime,
            # so `proven_by_module` must export a resolver and it must refuse
            # every mime this cell advertises — that refusal IS the claim.
            assert hasattr(module, cell.proven_by), cell.key
            resolve = getattr(module, cell.proven_by)
            for mime in cell.mimes:
                assert resolve(mime) is None, f"{cell.key}: {mime} must stay refused"
        else:
            assert hasattr(module, cell.proven_by), cell.key


def test_each_cell_carries_a_claim_and_a_disclosure() -> None:
    for cell in MATRIX:
        assert cell.claim.strip(), cell.key
        assert cell.disclosure.strip(), cell.key
        if cell.outcome is FormatOutcome.REJECTED:
            assert cell.rejection and "unsupported_mime_type" in cell.rejection, cell.key
        if cell.runtime_caveat is not None:
            assert cell.runtime_caveat.strip(), cell.key


def test_rejected_mimes_are_advertised_and_refused_by_the_registry() -> None:
    """XLSX and images are refused BEFORE any parser sees the bytes."""
    assert XLSX_MIME in REJECTED_MIMES
    assert PNG_MIME in REJECTED_MIMES
    assert "image/jpeg" in REJECTED_MIMES
    for mime in sorted(REJECTED_MIMES):
        assert parser_for_mime(mime) is None, mime
        cell = cell_for_mime(mime)
        assert cell is not None and cell.outcome is FormatOutcome.REJECTED, mime
        assert parser_chain_for(mime) == (), mime


async def test_rejected_cells_raise_a_typed_refusal_not_a_silent_noop() -> None:
    from ingestion.normalize import UnsupportedMimeTypeError

    for name, mime in (("sheet.xlsx", XLSX_MIME), ("scan.png", PNG_MIME)):
        with pytest.raises(UnsupportedMimeTypeError):
            await parse_with_precedence(_fixture(name), mime_type=mime, filename=name)


def test_runtime_support_is_a_probe_of_the_proving_module() -> None:
    support = runtime_support()
    assert set(support) == set(MATRIX_KEYS)
    # In this deployment the proving modules import; the probe must say so.
    assert support["pdf_digital"] is True and support["docx"] is True
    assert support["pdf_scanned"] is True


# --------------------------------------------------------------------------------------
# 1a. Cell proofs
# --------------------------------------------------------------------------------------


async def test_cell_pdf_digital_reads_the_whole_document_with_pages() -> None:
    parsed, report = await parse_with_precedence(
        _fixture("pdf_digital.pdf"), mime_type=PDF_MIME, filename="pdf_digital.pdf"
    )
    assert parsed.elements, "digital PDF produced no elements"
    assert {element.page for element in parsed.elements} == {1, 2}
    # The cell's actual claim is "the whole document is read, with the page map
    # preserved" — NOT "the first line is a TITLE element". Title detection is
    # parser-specific and is deliberately not asserted here, because the two
    # parsers that can serve this cell disagree and neither is wrong:
    #   * pypdf (`PdfTextParser`) heuristically promotes the first line of page 1
    #     to TITLE, and `test_pdf_text_preserves_pages_and_sections` (pd001)
    #     already pins that heuristic on its own fixture;
    #   * docling on THIS deployment runs `NativePdfPipelineOptions` (torch-free
    #     — the `DocumentConverter` default needs `torch`, absent here), and that
    #     pipeline emits only `label='text'`, so the adapter's TITLE branch is
    #     unreachable for any PDF. It labels the headings 'text' too. DOCX is the
    #     same: docling returns the Title-styled line as `heading`.
    # Asserting TITLE here would pin a claim no docling parse of this fixture can
    # satisfy, i.e. a cell advertised as supported that fails on the docling path.
    # What IS invariant across both parsers is asserted instead.
    first = parsed.elements[0]
    assert first.text == "Format Matrix Digital PDF Fixture" and first.page == 1
    # The numbered heading must open a section on every parser in the chain.
    # The section NAME differs by parser and neither is wrong: docling keeps the
    # numbering ('1. Introduction'), pypdf strips it ('Introduction') — so the
    # invariant is the word, not the exact string.
    assert any(
        element.section is not None and "Introduction" in element.section
        for element in parsed.elements
    ), "the numbered heading must open a section on every parser in the chain"
    # Every element carries its page: the page map is the documented claim and
    # it holds on both paths.
    assert all(element.page is not None for element in parsed.elements)
    assert report.used in {type(parser).__name__ for parser in parser_chain_for(PDF_MIME)}
    assert report.primary == "DoclingParser"
    assert set(report.as_metadata()) == {"primary", "used", "fallback_used", "attempts"}


async def test_cell_pdf_tables_keeps_tables_instead_of_text_soup() -> None:
    parsed = await PdfTableParser().parse(
        _fixture("pdf_tables_heavy.pdf"), mime_type=PDF_MIME
    )
    tables = [element for element in parsed.elements if element.type is ElementType.TABLE]
    assert len(tables) == 3, "one ruled table per page must survive as a TABLE element"
    for table in tables:
        assert "Revenue" in table.text and "Margin" in table.text
        # Rows intact and still separated as rows — a 12-row table cannot
        # collapse into one blob. (clean_text normalises newlines to spaces,
        # so the row separator the adapter emits is " | ".)
        assert table.text.count(" | ") == 11, "header + 12 rows"
    texts = [element for element in parsed.elements if element.type is ElementType.TEXT]
    assert texts, "body text outside a table must stay TEXT, not be swallowed"
    assert any("not inside any table" in element.text for element in texts)


async def test_cell_pdf_scanned_is_detected_as_fully_lost() -> None:
    report = pdf_text_layer_report(_fixture("pdf_scanned.pdf"))
    assert report is not None
    assert report.page_count == 3
    assert report.pages_with_text == ()
    assert report.lost_pages == (1, 2, 3)
    assert report.fully_lost is True
    assert report.partially_lost is False
    disclosure = report.disclosure()
    assert disclosure["reason"] == "no_text_layer"
    assert disclosure["lost_pages"] == [1, 2, 3]
    assert disclosure["readable_pages"] == 0


async def test_cell_pdf_scanned_partial_case_names_the_lost_page() -> None:
    report = pdf_text_layer_report(_fixture("pdf_mixed_scan.pdf"))
    assert report is not None
    assert report.pages_with_text == (1,)
    assert report.lost_pages == (2,)
    assert report.fully_lost is False and report.partially_lost is True


async def test_cell_pdf_scanned_text_adapter_reports_pages_it_could_not_read() -> None:
    parsed = await PdfTextParser().parse(
        _fixture("pdf_mixed_scan.pdf"), mime_type=PDF_MIME
    )
    assert {element.page for element in parsed.elements} == {1}
    assert parsed.metadata["pages_without_text"] == [2]


async def test_cell_docx_keeps_headings_sections_and_tables() -> None:
    parsed = await DocxParser().parse(_fixture("docx_tables.docx"), mime_type=DOCX_MIME)
    kinds = [element.type for element in parsed.elements]
    assert ElementType.TITLE in kinds
    assert ElementType.HEADING in kinds
    assert ElementType.TABLE in kinds
    table = next(element for element in parsed.elements if element.type is ElementType.TABLE)
    assert "Revenue" in table.text and table.section == "Monthly ledger"


def test_cell_xlsx_and_image_fixtures_are_real_content_not_garbage() -> None:
    """The refusal is a refusal of *content*: the fixtures must be valid files."""
    from io import BytesIO
    from zipfile import ZipFile

    from PIL import Image

    archive = ZipFile(BytesIO(_fixture("sheet.xlsx")))
    assert "xl/worksheets/sheet1.xml" in archive.namelist()
    assert b"Revenue" in archive.read("xl/worksheets/sheet1.xml")

    with Image.open(BytesIO(_fixture("scan.png"))) as image:
        assert image.format == "PNG" and image.size == (32, 32)


# --------------------------------------------------------------------------------------
# 2. Budgets enforced PRE-expansion
# --------------------------------------------------------------------------------------


class _ExpansionBombParser(DocumentParser):
    """Emits elements for ever — the pathological input the budgets exist for."""

    supported_mime_types = frozenset({"text/plain"})

    def __init__(self, chunk: str = "x" * 1000) -> None:
        self.chunk = chunk
        self.emitted = 0

    async def parse(self, data, *, mime_type, filename=None, metadata=None):
        elements = []
        while True:
            elements.append(make_element(ElementType.TEXT, self.chunk))
            self.emitted += 1


def _budgets(**overrides: float | int) -> ParseBudgets:
    base: dict[str, float | int] = {
        "max_seconds": 10.0,
        "max_memory_bytes": 1 << 40,
        "max_expanded_chars": 10_000_000,
        "max_output_elements": 10_000_000,
    }
    base.update(overrides)
    return ParseBudgets(
        max_seconds=float(base["max_seconds"]),
        max_memory_bytes=int(base["max_memory_bytes"]),
        max_expanded_chars=int(base["max_expanded_chars"]),
        max_output_elements=int(base["max_output_elements"]),
    )


async def test_expanded_text_budget_trips_mid_expansion_not_after_it() -> None:
    """The decisive proof: the parse is abandoned AT the budget, not afterwards.

    ``_ExpansionBombParser`` never terminates on its own — it would expand for
    ever. If the budget were enforced by counting elements *after* the parse,
    this test would hang; it returns in milliseconds with the exact amount of
    text the budget allows and not one element more.
    """
    per_element = 1000
    budget_chars = 100_000
    parser = _ExpansionBombParser(chunk="x" * per_element)
    with pytest.raises(ParseBudgetExceeded) as excinfo:
        await parse_with_budgets(
            parser, b"x", mime_type="text/plain", budgets=_budgets(max_expanded_chars=budget_chars)
        )
    assert excinfo.value.code == EXPANDED_TEXT_BUDGET
    # The guard trips on the first element that would push it PAST the budget,
    # so the parse is abandoned having produced exactly the budget, no more.
    assert parser.emitted == budget_chars // per_element
    assert parser.emitted * per_element == budget_chars


async def test_output_element_budget_trips_mid_expansion() -> None:
    budget_elements = 250
    parser = _ExpansionBombParser(chunk="y")
    with pytest.raises(ParseBudgetExceeded) as excinfo:
        await parse_with_budgets(
            parser,
            b"x",
            mime_type="text/plain",
            budgets=_budgets(max_output_elements=budget_elements),
        )
    assert excinfo.value.code == OUTPUT_BUDGET
    assert parser.emitted == budget_elements


async def test_time_budget_abandons_the_parse_at_the_deadline() -> None:
    import time

    class _SlowParser(DocumentParser):
        supported_mime_types = frozenset({"text/plain"})

        async def parse(self, data, *, mime_type, filename=None, metadata=None):
            while True:
                time.sleep(0.02)  # noqa: ASYNC251 - the block IS the behaviour under test
                make_element(ElementType.TEXT, "slow")

    started = time.monotonic()
    with pytest.raises(ParseBudgetExceeded) as excinfo:
        await parse_with_budgets(
            _SlowParser(), b"x", mime_type="text/plain", budgets=_budgets(max_seconds=0.3)
        )
    assert excinfo.value.code == "parse_time_budget"
    assert time.monotonic() - started < 5.0


async def test_memory_budget_is_observed_by_the_samplers_accounting() -> None:
    """Pin the accounting, not a real 1 GiB allocation."""
    guard = ParseBudgetGuard(_budgets(max_memory_bytes=1))
    guard.start()
    try:
        guard.peak_rss_bytes = 4096  # simulate a sample over the 1-byte budget
        with pytest.raises(ParseBudgetExceeded) as excinfo:
            guard.check()
        assert excinfo.value.code == "parse_memory_budget"
    finally:
        guard.close()


def test_every_budget_code_is_in_the_shared_set_and_never_retryable() -> None:
    from jobs.ingest_pipeline import IngestPipelineError

    assert BUDGET_CODES == {
        "parse_time_budget",
        "parse_memory_budget",
        "parse_expanded_text_budget",
        "parse_output_budget",
    }
    for code in BUDGET_CODES:
        # Deterministic: retrying can only spend the same budget again.
        assert IngestPipelineError(code, "budget", retryable=False).retryable is False


def test_the_pipeline_maps_a_budget_breach_to_a_non_retryable_typed_failure() -> None:
    """Source pin (same style as the T-V1-019 gate test): no swallowed budget."""
    import inspect

    from jobs import ingest_pipeline

    source = inspect.getsource(ingest_pipeline.process_job)
    assert "except ParseBudgetExceeded as exc:" in source
    assert "raise IngestPipelineError(exc.code, exc.message, retryable=False) from exc" in source
    # ...and it must come before the generic catch-all, or it would be eaten.
    assert source.index("except ParseBudgetExceeded as exc:") < source.index(
        "except Exception as exc:"
    )


async def test_a_budget_breach_never_falls_through_to_the_next_parser() -> None:
    """Budgets are a property of the input and host, not of one parser."""
    guard_probe: list[str] = []

    class _BreachingParser(DocumentParser):
        supported_mime_types = frozenset({"text/plain"})

        async def parse(self, data, *, mime_type, filename=None, metadata=None):
            guard_probe.append("tried")
            raise ParseBudgetExceeded(EXPANDED_TEXT_BUDGET, "over budget")

    from ingestion.adapters import registry

    original = dict(registry.PARSER_PRECEDENCE)
    try:
        registry.PARSER_PRECEDENCE["text/plain"] = (_BreachingParser(), _BreachingParser())
        with pytest.raises(ParseBudgetExceeded):
            await parse_with_precedence(b"x", mime_type="text/plain", budgets=_budgets())
        assert guard_probe == ["tried"], "the second parser must never be reached"
    finally:
        registry.PARSER_PRECEDENCE.clear()
        registry.PARSER_PRECEDENCE.update(original)


def test_the_guard_is_the_only_element_checkpoint_in_the_adapters() -> None:
    """Every adapter funnels through make_element — one choke point, not eight."""
    import inspect

    from ingestion.adapters import csv, docling, docx, html, markdown, pdf_table, pdf_text, txt

    for module in (csv, docling, docx, html, markdown, pdf_table, pdf_text, txt):
        source = inspect.getsource(module)
        assert "make_element(" in source, module.__name__
        assert "DocumentElement(" not in source, module.__name__


def test_make_element_accounts_to_the_active_guard_only_when_one_is_installed() -> None:
    budgets = _budgets(max_output_elements=3)
    guard = ParseBudgetGuard(budgets)
    guard.start()
    token_guard = __import__("ingestion.budget", fromlist=["_ACTIVE_GUARD"])._ACTIVE_GUARD.set(
        guard
    )
    try:
        for index in range(3):
            make_element(ElementType.TEXT, f"element {index}")
        assert guard.output_elements == 3 and guard.expanded_chars == sum(
            len(f"element {index}") for index in range(3)
        )
        with pytest.raises(ParseBudgetExceeded):
            make_element(ElementType.TEXT, "one too many")
    finally:
        __import__("ingestion.budget", fromlist=["_ACTIVE_GUARD"])._ACTIVE_GUARD.reset(token_guard)
        guard.close()
    # Outside a guarded parse the behaviour is unchanged.
    assert make_element(ElementType.TEXT, "unguarded").text == "unguarded"


# --------------------------------------------------------------------------------------
# 3. Partial -> needs_review; missing regions disclosed, never confident
# --------------------------------------------------------------------------------------


def test_a_fully_read_document_finishes_ready() -> None:
    status, code, message = terminal_job_state([])
    assert (status, code, message) == (JobStatus.READY.value, None, None)


def test_a_partially_read_document_never_finishes_ready() -> None:
    status, code, message = terminal_job_state(
        [{"reason": "no_text_layer", "lost_pages": [2], "page_count": 2, "readable_pages": 1}]
    )
    assert status == JobStatus.NEEDS_REVIEW.value
    assert code == "NEEDS_REVIEW"
    assert message is not None and "2" in message and "NOT read" in message


def test_the_pipeline_actually_parks_a_partial_read_needs_review() -> None:
    """Source pin: the pure rule above must be the rule the pipeline applies.

    ``terminal_job_state`` is the specification; ``process_job`` applies it
    inline (the T-V1-019 gate test pins that transition by source order, so it
    cannot be delegated without breaking that pin). This asserts the inline
    branch says the same thing for both cases.
    """
    import inspect

    from jobs import ingest_pipeline

    source = inspect.getsource(ingest_pipeline.process_job)
    assert "job.status = JobStatus.NEEDS_REVIEW.value" in source
    assert 'job.error_code = "NEEDS_REVIEW"' in source
    assert "job.status = JobStatus.READY.value" in source
    assert "needs_review_reasons_for(lost_regions)" in source


def test_needs_review_is_a_registered_job_state_with_the_disclosure_code() -> None:
    assert JobStatus.NEEDS_REVIEW.value == "needs_review"
    assert ReasonCode.NEEDS_REVIEW in JOB_STATUS_REASON_CODES[JobStatus.NEEDS_REVIEW]
    assert ReasonCode.NEEDS_REVIEW.value == "NEEDS_REVIEW"


def test_parse_disclosure_rides_on_every_chunk_only_when_regions_were_lost() -> None:
    from jobs.ingest_pipeline import (
        PARSE_LOST_REGIONS_KEY,
        PARSE_PARTIAL_KEY,
        PARSE_REPORT_KEY,
        parse_disclosure,
    )

    report = ParseReport(
        primary="DoclingParser",
        used="PdfTextParser",
        attempts=(ParseAttempt(parser="DoclingParser", outcome="unavailable"),),
    )
    clean = parse_disclosure(report, [])
    assert PARSE_PARTIAL_KEY not in clean
    assert clean[PARSE_REPORT_KEY]["fallback_used"] is True

    lost = [{"reason": "no_text_layer", "lost_pages": [2], "page_count": 2}]
    partial = parse_disclosure(report, lost)
    assert partial[PARSE_PARTIAL_KEY] is True
    assert partial[PARSE_LOST_REGIONS_KEY] == lost


def test_fallback_disclosure_is_recorded_when_docling_is_not_the_parser_that_answered() -> None:
    report = ParseReport(
        primary="DoclingParser",
        used="PdfTextParser",
        attempts=(ParseAttempt(parser="DoclingParser", outcome="unavailable"),),
    )
    metadata = report.as_metadata()
    assert metadata == {
        "primary": "DoclingParser",
        "used": "PdfTextParser",
        "fallback_used": True,
        "attempts": [{"parser": "DoclingParser", "outcome": "unavailable", "error": None}],
    }


# -- the answer layer --------------------------------------------------------------------


def _answer(status=AnswerStatus.ANSWERED, level="high"):
    from answer.models import Answer, Confidence, ConfidenceLevel

    return Answer(
        answer="Revenue was 1450.",
        status=status,
        confidence=Confidence(
            level=ConfidenceLevel(level),
            basis="Answer grounded in 1 verified, substantiated citation(s)",
        ),
    )


def _item_with_lost_regions():
    return make_item("Revenue was 1450 on page 1").model_copy(
        update={
            "metadata": {
                "parse_partial": True,
                "parse_lost_regions": [
                    {
                        "reason": "no_text_layer",
                        "lost_pages": [2],
                        "page_count": 2,
                        "readable_pages": 1,
                    }
                ],
            }
        }
    )


def test_an_answer_over_an_omitted_region_is_never_confident() -> None:
    from answer.engine import AnswerEngine
    from answer.models import ConfidenceLevel

    answered = AnswerEngine._disclose_source(_answer(), [_item_with_lost_regions()])
    assert answered.status is AnswerStatus.NEEDS_REVIEW
    assert answered.reason_code == ReasonCode.NEEDS_REVIEW.value
    assert answered.confidence.level is ConfidenceLevel.MEDIUM, "HIGH claims completeness"
    assert any("[2]" in limitation for limitation in answered.limitations)
    assert "NOT available" in answered.limitations[-1]


def test_an_answer_over_a_fully_read_source_is_untouched() -> None:
    from answer.engine import AnswerEngine
    from answer.models import ConfidenceLevel

    clean_item = make_item("Revenue was 1450 on page 1")
    base = _answer()
    answered = AnswerEngine._disclose_source(base, [clean_item])
    assert answered is base, "a fully-read source must not be relabelled at all"
    assert answered.status is AnswerStatus.ANSWERED
    assert answered.confidence.level is ConfidenceLevel.HIGH
    assert answered.limitations == []
    assert answered.reason_code is None


def test_an_abstention_keeps_its_reason_but_still_discloses_the_lost_region() -> None:
    from answer.engine import AnswerEngine

    abstained = _answer(status=AnswerStatus.INSUFFICIENT_EVIDENCE, level="low")
    result = AnswerEngine._disclose_source(abstained, [_item_with_lost_regions()])
    assert result.status is AnswerStatus.INSUFFICIENT_EVIDENCE, "an abstention is not a claim"
    assert result.reason_code is None
    assert any("[2]" in limitation for limitation in result.limitations)


def test_the_lost_region_metadata_key_is_the_one_ingestion_writes() -> None:
    """The two sides must agree on the key or the disclosure never arrives."""
    import inspect

    from answer.engine import AnswerEngine
    from jobs.ingest_pipeline import PARSE_LOST_REGIONS_KEY

    assert PARSE_LOST_REGIONS_KEY == "parse_lost_regions"
    collect_source = inspect.getsource(AnswerEngine._collect_lost_regions)
    assert "parse_lost_regions" in collect_source


# --------------------------------------------------------------------------------------
# 4. The docs page matches the code
# --------------------------------------------------------------------------------------


def _docs_matrix_rows() -> dict[str, dict[str, str]]:
    """Parse the docs matrix table: ``key -> {outcome, fixture, proven_by}``."""
    text = DOCS_PAGE.read_text(encoding="utf-8")
    rows: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        key = cells[0].strip("`")
        if key not in MATRIX_KEYS:
            continue
        rows[key] = {
            "outcome": cells[2].strip("`"),
            "proven_by": cells[3].strip("`"),
            "fixture": cells[4],
        }
    return rows


def test_docs_page_exists_and_lists_every_cell_with_its_outcome() -> None:
    assert DOCS_PAGE.is_file(), f"missing docs matrix page: {DOCS_PAGE}"
    rows = _docs_matrix_rows()
    assert set(rows) == set(MATRIX_KEYS), "docs table must carry exactly the six cells"
    for key, row in rows.items():
        cell = MATRIX_BY_KEY[key]
        assert row["outcome"] == cell.outcome.value, key
        proven_by = row["proven_by"]
        assert proven_by is not None and proven_by == cell.proven_by, key
        fixture = row["fixture"]
        cell_fixture = cell.fixture
        assert fixture is not None and cell_fixture is not None and cell_fixture in fixture, key


def test_docs_page_documents_every_budget_and_its_env_var() -> None:
    text = DOCS_PAGE.read_text(encoding="utf-8")
    for variable, code in (
        ("INGEST_PARSE_TIMEOUT_SECONDS", "parse_time_budget"),
        ("INGEST_PARSE_MAX_MEMORY_MB", "parse_memory_budget"),
        ("INGEST_PARSE_MAX_EXPANDED_CHARS", "parse_expanded_text_budget"),
        ("INGEST_PARSE_MAX_OUTPUT_ELEMENTS", "parse_output_budget"),
    ):
        assert variable in text, variable
        assert code in text, code
    # The budget defaults in the docs are the defaults in code.
    from ingestion.budget import budgets_from_env

    defaults = budgets_from_env({})
    assert str(int(defaults.max_seconds)) in text
    assert str(defaults.max_memory_bytes // (1024 * 1024)) in text
    assert f"{defaults.max_expanded_chars:,}".replace(",", "_") in text


def test_docs_page_states_the_disclosed_limits_rather_than_hiding_them() -> None:
    text = DOCS_PAGE.read_text(encoding="utf-8")
    assert "cannot kill a thread" in text
    assert "pre-expansion" in text
    assert "OCR is not part of this deployment" in text
    assert "confident answers drawn from an omitted region: **0**" in text


def test_docs_page_never_promotes_a_partial_or_rejected_cell_to_supported() -> None:
    text = DOCS_PAGE.read_text(encoding="utf-8")
    matrix_block = text[: text.index("## Parse budgets")]
    for cell in MATRIX:
        match = re.search(rf"^\| `{cell.key}` \|.*$", matrix_block, re.MULTILINE)
        assert match, cell.key
        assert f"`{cell.outcome.value}`" in match.group(0), cell.key
