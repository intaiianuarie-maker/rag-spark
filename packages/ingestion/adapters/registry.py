"""MIME -> DocumentParser registry (M2-001c).

Keeps parser construction in one place so the worker pipeline (and the upload
endpoint's mime validation) share it without importing individual adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..budget import ParseBudgetExceeded, ParseBudgets, parse_with_budgets
from ..interfaces import DocumentParser, ParsedDocument
from ..normalize import ParseError, UnsupportedMimeTypeError
from .csv import CsvParser
from .docling import DoclingParser
from .docx import DocxParser
from .html import HtmlParser
from .markdown import MarkdownParser
from .pdf_table import PdfTableParser
from .pdf_text import PdfTextParser
from .txt import TextParser

# T-V1-030 — accepted parser precedence, recorded first in DECISIONS.md
# ("T-V1-030 parser precedence" row) and pinned by
# tests/unit/test_parser_precedence.py. It reconciles frozen PD-001
# (pypdf/pdfplumber primary, decisions/PD-001-v1-parser-stack.md) with
# V1-COMPLETION-PLAN §2.5 "Docling-first with fallback", per the AGENTS.md
# source-of-truth priority (DECISIONS.md outranks PLAN) and the C19 ruling
# (docling is covered by DECISIONS.md; PD-001 stays frozen):
#   1. Docling-first: DoclingParser is PRIMARY for the layout formats
#      DECISIONS.md declares docling primary for and that docling serves in
#      this deployment — PDF, DOCX, HTML.
#   2. PD-001 fast path stays PRIMARY for the plain-text family (markdown,
#      txt, csv): docling cannot parse text/* here (its `marko` format extra
#      is absent) and adds nothing for plain text, so docling-first on those
#      mimes would fail every TXT/MD upload.
#   3. Registration stays first-wins, so a mime resolves to its accepted
#      primary and every later claimant stays a FALLBACK: PARSER_PRECEDENCE
#      holds the full chain in accepted order (pdf -> docling -> pypdf ->
#      pdfplumber, etc.), disclosed in DECISIONS.md and available to a
#      runtime fallback router (T-V1-031). parser_for_mime returns the
#      primary only; fallbacks also remain directly importable.
DOCLING_PRIMARY_MIMES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "text/html",
    }
)

_DOCLING = DoclingParser()

_PARSERS: tuple[DocumentParser, ...] = (
    _DOCLING,
    PdfTextParser(),
    PdfTableParser(),
    DocxParser(),
    HtmlParser(),
    MarkdownParser(),
    CsvParser(),
    TextParser(),
)

PARSER_REGISTRY: dict[str, DocumentParser] = {}
PARSER_PRECEDENCE: dict[str, tuple[DocumentParser, ...]] = {}
for _parser in _PARSERS:
    for _mime in _parser.supported_mime_types:
        if _parser is _DOCLING and _mime not in DOCLING_PRIMARY_MIMES:
            continue  # docling claims text/* it cannot serve here -> PD-001 fast path
        _chain = PARSER_PRECEDENCE.get(_mime, ())
        PARSER_PRECEDENCE[_mime] = (*_chain, _parser)
        PARSER_REGISTRY.setdefault(_mime, _parser)


def parser_for_mime(mime_type: str | None) -> DocumentParser | None:
    if not mime_type:
        return None
    return PARSER_REGISTRY.get(mime_type)


def supported_mime_types() -> frozenset[str]:
    return frozenset(PARSER_REGISTRY)


def parser_chain_for(mime_type: str | None) -> tuple[DocumentParser, ...]:
    """The full T-V1-030 precedence chain for a mime (primary first), or ``()``.

    This is the seam T-V1-030 left open for T-V1-031: ``parser_for_mime``
    answers "who is the accepted primary", this answers "who may be tried, in
    what order, when the primary cannot read this particular file".
    """
    if not mime_type:
        return ()
    return PARSER_PRECEDENCE.get(mime_type, ())


@dataclass(frozen=True)
class ParseAttempt:
    """One parser that was tried, and whether it answered."""

    parser: str
    outcome: str  # "ok" | "unavailable" | "failed"
    error: str | None = None


@dataclass(frozen=True)
class ParseReport:
    """What the chain did. Always disclosed, never invented.

    ``used`` is the adapter that actually produced the elements. ``primary`` is
    the accepted primary from T-V1-030. When they differ the parse is a
    **fallback**, and every consumer (chunk metadata, job disclosure, the
    answer layer) is told so rather than being left to assume the primary ran.
    """

    primary: str
    used: str
    attempts: tuple[ParseAttempt, ...]

    @property
    def fallback_used(self) -> bool:
        return self.used != self.primary

    def as_metadata(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "used": self.used,
            "fallback_used": self.fallback_used,
            "attempts": [attempt.__dict__ for attempt in self.attempts],
        }


#: Key under which the parse report rides on ``ParsedDocument.metadata``.
PARSE_REPORT_KEY = "parse"


async def parse_with_precedence(
    data: bytes,
    *,
    mime_type: str,
    filename: str | None = None,
    metadata: dict[str, Any] | None = None,
    budgets: ParseBudgets | None = None,
) -> tuple[ParsedDocument, ParseReport]:
    """Parse ``data`` down the T-V1-030 chain, disclosing exactly what happened.

    Fallback is deliberately narrow and always recorded:

    * a parser that **cannot serve this file** (its own import is missing, or
      it reports the bytes are not its format) hands over to the next in chain;
    * a parser that fails for any other reason **ends the chain** — silently
      trying the next adapter would hide a real parse failure behind whatever
      the fallback managed to salvage, which is the dishonesty this ticket
      exists to prevent;
    * a :class:`~ingestion.budget.ParseBudgetExceeded` always propagates. The
      budgets describe the file and the host, not one adapter, so the next
      adapter would only spend the same budget again.

    Raises ``ParseError`` (from :mod:`ingestion.normalize`) when nothing in the
    chain can read the file, naming the primary first.
    """
    chain = parser_chain_for(mime_type)
    if not chain:
        raise UnsupportedMimeTypeError(f"unsupported mime type: {mime_type}")

    primary = type(chain[0]).__name__
    attempts: list[ParseAttempt] = []
    first_failure: tuple[str, str] | None = None

    for parser in chain:
        name = type(parser).__name__
        try:
            parsed = await parse_with_budgets(
                parser,
                data,
                mime_type=mime_type,
                filename=filename,
                metadata=metadata,
                budgets=budgets,
            )
        except ParseBudgetExceeded:
            raise
        except Exception as exc:
            if first_failure is None:
                first_failure = (name, f"{type(exc).__name__}: {exc}")
            attempts.append(
                ParseAttempt(parser=name, outcome="failed", error=f"{type(exc).__name__}: {exc}")
            )
            if _cannot_serve(exc):
                continue  # documented hand-over: this adapter is not for these bytes
            break
        attempts.append(ParseAttempt(parser=name, outcome="ok"))
        parsed.metadata = {
            **(parsed.metadata or {}),
            PARSE_REPORT_KEY: ParseReport(
                primary=primary, used=name, attempts=tuple(attempts)
            ).as_metadata(),
        }
        return parsed, ParseReport(primary=primary, used=name, attempts=tuple(attempts))

    tried = ", ".join(attempt.parser for attempt in attempts)
    raise ParseError(
        f"no parser in the accepted chain could read {mime_type} "
        f"(primary {primary}): {first_failure[0]} raised {first_failure[1]}"
        if first_failure is not None
        else f"no parser in the accepted chain could read {mime_type} (tried {tried})"
    )


#: Signals a parser raises to say "these bytes are not my format" — the only
#: condition that earns a documented hand-over to the next adapter in the chain.
_CANNOT_SERVE_MARKERS = (
    "not available",
    "is not a valid",
    "could not read",
    "file has not been decoded",
    "unsupported mime type",
    "produced no extractable",
)


def _cannot_serve(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CANNOT_SERVE_MARKERS)

