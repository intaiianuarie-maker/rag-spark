"""Contract checks for the parser interface (`ingestion.interfaces.DocumentParser`).

Test-only (TASK-M0-004). Call it with any `DocumentParser`:

- the M0 harness passes a fake to lock the *interface*;
- M2-001a passes its pypdf/docx/bs4/markdown-it adapters together with the six
  PD-001 fixtures declared in `tests/fixtures/pd001/expected_elements.json`,
  which is where `DocumentElement(page/section)` preservation is asserted.

Nothing here parses anything: no third-party parser types may appear, and every
failure is an explicit `AssertionError` so the suite can be reused without
pytest.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from .interfaces import DocumentElement, DocumentParser, ParsedDocument


def _fail(message: str) -> None:
    raise AssertionError(message)


def _require_instance(value: object, expected_type: type, what: str) -> None:
    """Runtime leak guard (module-boundaries): static types can't catch a casted return."""
    if not isinstance(value, expected_type):
        _fail(f"{what}: expected {expected_type.__name__}, got {type(value).__name__}")


async def _expect_error(awaitable: Awaitable[object], what: str) -> None:
    """Unsupported input must raise loudly (never return a silent empty document)."""
    try:
        result = await awaitable
    except Exception:  # any loud failure satisfies the contract
        return
    _fail(f"{what}: expected an exception, but returned {result!r}")


@dataclass(frozen=True)
class ExpectedElement:
    """One expectation from `tests/fixtures/pd001/expected_elements.json`."""

    type: str
    text_contains: str
    page: int | None = None
    section_contains: str | None = None
    metadata_contains: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParserFixture:
    """A fixture file plus the normalized elements it must produce."""

    fixture_id: str
    data: bytes
    mime_type: str
    filename: str
    expected_elements: tuple[ExpectedElement, ...]
    excluded_text: tuple[str, ...] = ()


def element_matches(element: DocumentElement, expected: ExpectedElement) -> bool:
    """Match rule documented in `expected_elements.json` (all fields must hold)."""
    if element.type.value != expected.type:
        return False
    if expected.text_contains not in element.text:
        return False
    if expected.page is not None and element.page != expected.page:
        return False
    if expected.section_contains is not None:
        if expected.section_contains not in (element.section or ""):
            return False
    for key, value in expected.metadata_contains.items():
        if element.metadata.get(key) != value:
            return False
    return True


async def check_document_parser_contract(
    parser: DocumentParser, fixture: ParserFixture
) -> ParsedDocument:
    """Run one fixture through a parser and assert the normalized contract."""
    result = await parser.parse(
        fixture.data, mime_type=fixture.mime_type, filename=fixture.filename
    )
    _require_instance(result, ParsedDocument, f"[{fixture.fixture_id}] parse return value")
    if not result.elements:
        _fail(f"[{fixture.fixture_id}] parse returned no elements")

    for element in result.elements:
        _require_instance(
            element, DocumentElement, f"[{fixture.fixture_id}] element type (module-boundaries)"
        )
        if not element.text:
            _fail(f"[{fixture.fixture_id}] element has empty text: {element}")
        if element.page is not None and element.page < 1:
            _fail(f"[{fixture.fixture_id}] element.page must be 1-based: {element.page}")

    for expected in fixture.expected_elements:
        if not any(element_matches(element, expected) for element in result.elements):
            rendered = [
                f"{element.type.value}:{element.text[:40]!r}@p{element.page}/{element.section!r}"
                for element in result.elements
            ]
            _fail(
                f"[{fixture.fixture_id}] no element matches {expected}; "
                f"got {len(result.elements)} elements: {rendered}"
            )

    for banned in fixture.excluded_text:
        if any(banned in element.text for element in result.elements):
            _fail(f"[{fixture.fixture_id}] boilerplate {banned!r} survived normalization")

    if parser.supported_mime_types and fixture.mime_type not in parser.supported_mime_types:
        _fail(
            f"[{fixture.fixture_id}] parser does not declare support for {fixture.mime_type}"
        )

    await _expect_error(
        parser.parse(fixture.data, mime_type="audio/mpeg", filename=fixture.filename),
        f"[{fixture.fixture_id}] parse with an unsupported mime_type",
    )
    await _expect_error(
        parser.parse(b"", mime_type=fixture.mime_type, filename=fixture.filename),
        f"[{fixture.fixture_id}] parse of empty bytes",
    )
    return result
