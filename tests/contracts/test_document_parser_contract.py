"""DocumentParser contract + the six PD-001 fixtures (`ingestion.interfaces`).

Three layers, so the suite is useful before *and* after M2-001a:

1. **structure** — the six fixture files exist and `expected_elements.json` is
   complete and self-consistent (runs today, no parser: TASK-M0-004
   "structure only, no parsing impl");
2. **interface** — a weight-free fake parser passes the reusable checker
   (`ingestion.contract_tests.check_document_parser_contract`) for all six;
3. **real parse** — once M2-001a registers parsers under the manifest's
   `parser_module` convention (`PARSER` or `create_parser()`), the *same*
   checker asserts `DocumentElement(page/section)` preservation on real output.
"""

from __future__ import annotations

import csv as csv_mod
import importlib
import importlib.util
import io
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from harness_fakes import FakeDocumentParser
from ingestion.contract_tests import (
    ExpectedElement,
    ParserFixture,
    check_document_parser_contract,
)
from ingestion.interfaces import DocumentElement, ElementType

pytestmark = [pytest.mark.unit, pytest.mark.contract]

#: The six fixtures TASK-M0-004 requires (PD-001 fixture plan).
REQUIRED_FIXTURE_IDS = frozenset(
    {
        "pdf_text_multipage",
        "pdf_table",
        "docx_headings_table",
        "html_article_nav",
        "markdown_structure",
        "csv_multilingual",
    }
)
ELEMENT_TYPES = frozenset(member.value for member in ElementType)
RAW_FORMATS = {"pdf", "docx", "html", "md", "csv", "txt"}

#: Every manifest fixture gets a fake-parser contract run; the six
#: REQUIRED_FIXTURE_IDS additionally gate the "structure" acceptance check.
_ALL_FIXTURE_IDS = sorted(
    entry["id"]
    for entry in json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "pd001"
            / "expected_elements.json"
        ).read_text(encoding="utf-8")
    )["fixtures"]
)


def load_manifest(fixtures_dir: Path) -> dict:
    path = fixtures_dir / "pd001" / "expected_elements.json"
    assert path.is_file(), f"missing PD-001 manifest: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixtures(fixtures_dir: Path) -> list[ParserFixture]:
    """Turn the manifest into the dataclass the reusable checker consumes."""
    directory = fixtures_dir / "pd001"
    loaded = []
    for entry in load_manifest(fixtures_dir)["fixtures"]:
        expected = tuple(
            ExpectedElement(
                type=raw["type"],
                text_contains=raw["text_contains"],
                page=raw.get("page"),
                section_contains=raw.get("section_contains"),
                metadata_contains=raw.get("metadata_contains", {}),
            )
            for raw in entry["expected_elements"]
        )
        loaded.append(
            ParserFixture(
                fixture_id=entry["id"],
                data=(directory / entry["file"]).read_bytes(),
                mime_type=entry["mime_type"],
                filename=entry["file"],
                expected_elements=expected,
                excluded_text=tuple(entry.get("excluded_text", ())),
            )
        )
    return loaded


def _canned_elements(fixture: ParserFixture) -> list[DocumentElement]:
    """Manifest expectations rendered as elements: proves the schema is complete."""
    return [
        DocumentElement(
            type=ElementType(expected.type),
            text=expected.text_contains,
            page=expected.page,
            section=expected.section_contains,
            metadata=dict(expected.metadata_contains),
        )
        for expected in fixture.expected_elements
    ]


def _module_spec(module_path: str) -> object | None:
    try:
        return importlib.util.find_spec(module_path)
    except ModuleNotFoundError:
        return None


def _real_parser(module_path: str):
    module = importlib.import_module(module_path)
    parser = getattr(module, "PARSER", None)
    if parser is None:
        factory = getattr(module, "create_parser", None)
        parser = factory() if callable(factory) else None
    if parser is None:
        pytest.fail(
            f"{module_path} must expose PARSER: DocumentParser or "
            f"create_parser() -> DocumentParser (harness convention, tests/README.md)"
        )
    return parser


# --------------------------------------------------------------------------------------
# 1. Structure (no parser required)
# --------------------------------------------------------------------------------------


def test_six_pd001_fixtures_exist_with_a_complete_manifest(fixtures_dir: Path) -> None:
    payload = load_manifest(fixtures_dir)
    assert payload["required_fixture_count"] == 6
    entries = payload["fixtures"]
    ids = [entry["id"] for entry in entries]
    assert len(ids) == len(set(ids)), f"duplicate fixture ids: {ids}"
    missing = REQUIRED_FIXTURE_IDS - set(ids)
    assert not missing, f"required PD-001 fixtures missing from manifest: {sorted(missing)}"

    directory = fixtures_dir / "pd001"
    for entry in entries:
        path = directory / entry["file"]
        assert path.is_file(), f"fixture file missing: {path}"
        assert path.stat().st_size > 0, f"fixture file is empty: {path}"
        assert entry["format"] in RAW_FORMATS, f"unknown format in {entry['id']}: {entry['format']}"
        assert entry["mime_type"], f"{entry['id']} declares no mime_type"
        assert entry["parser_module"].startswith("ingestion.adapters."), (
            f"{entry['id']} parser_module must stay inside ingestion/: {entry['parser_module']}"
        )
        assert entry["expected_elements"], f"{entry['id']} declares no expected elements"
        for expected in entry["expected_elements"]:
            assert expected["type"] in ELEMENT_TYPES, (
                f"{entry['id']} unknown element type: {expected['type']}"
            )
            assert expected["text_contains"].strip(), f"{entry['id']} empty text_contains"
            if expected.get("page") is not None:
                assert isinstance(expected["page"], int) and expected["page"] >= 1

        raw = path.read_bytes()
        # `raw_contains` is checked against the bytes *as the format exposes them*:
        # a docx is a zip container, so its text lives in word/document.xml.
        haystack = raw
        if entry["format"] == "docx":
            with ZipFile(path) as archive:
                haystack = archive.read("word/document.xml")
        for needle in entry.get("raw_contains", []):
            assert needle.encode("utf-8") in haystack, (
                f"{entry['id']}: raw_contains {needle!r} not found in {entry['file']}"
            )

        if entry["format"] == "pdf":
            assert raw.startswith(b"%PDF-"), f"{entry['file']} is not a PDF"
            assert raw.rstrip().endswith(b"%%EOF"), f"{entry['file']} has no PDF EOF marker"
        elif entry["format"] == "docx":
            with ZipFile(path) as archive:
                assert "word/document.xml" in archive.namelist(), "docx has no document part"
        elif entry["format"] == "html":
            assert b"<article>" in raw and b"<h1>" in raw, "html fixture has no article/h1"
        elif entry["format"] == "md":
            assert raw.startswith(b"# "), "markdown fixture must start with an H1"
        elif entry["format"] == "csv":
            assert raw.splitlines()[0] == b"id,title,body,lang", "csv header row changed"


def test_manifest_declares_page_and_section_where_the_source_has_them(
    fixtures_dir: Path,
) -> None:
    for entry in load_manifest(fixtures_dir)["fixtures"]:
        expectations = entry["expected_elements"]
        if entry["expect_page_preserved"]:
            assert entry["page_count"], f"{entry['id']} must declare page_count"
            assert any(exp.get("page") for exp in expectations), (
                f"{entry['id']} expects page preservation but declares no page expectations"
            )
        if entry["expect_section_preserved"]:
            assert any(exp.get("section_contains") for exp in expectations), (
                f"{entry['id']} expects section preservation but declares no section expectations"
            )


def test_csv_fixture_carries_ro_and_pt_rows(fixtures_dir: Path) -> None:
    path = fixtures_dir / "pd001" / "csv_multilingual.csv"
    rows = list(csv_mod.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
    assert [row["lang"] for row in rows] == ["ro", "pt", "en", "ro"]
    assert "Bună ziua" in rows[0]["body"], "RO row lost its diacritics"
    assert "Olá" in rows[1]["body"], "PT row lost its diacritics"
    assert "," in rows[0]["title"], "quoted-comma cell was not preserved"


def test_utf8_fixtures_keep_diacritics(fixtures_dir: Path) -> None:
    directory = fixtures_dir / "pd001"
    html = (directory / "html_article_nav.html").read_text(encoding="utf-8")
    md = (directory / "markdown_structure.md").read_text(encoding="utf-8")
    assert "Secțiunea în română" in html and "Secção em português" in html
    assert "Secțiunea în română" in md and "recuperação de informação" in md


def test_cp1252_txt_fixture_is_intentionally_not_utf8(fixtures_dir: Path) -> None:
    """Pins the encoding-detection fixture (charset-normalizer path).

    `txt_cp1252.txt` must stay Windows-1252 encoded — if it were ever re-saved
    as UTF-8 the charset-detection contract would silently stop being exercised
    (code2 M2-001a review, Minor #3 / charset placement note). It contains
    byte `0x83` ("ƒ" in cp1252), which Central-European codepages reject, so
    detection is deterministic instead of the cp1250 tie-break that maps
    ã -> ă (observed live; see the M2-001a test report).
    """
    raw = (fixtures_dir / "pd001" / "txt_cp1252.txt").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    decoded = raw.decode("cp1252")
    assert "acentuação portuguesa" in decoded and "glifo ƒ exclusivo" in decoded


# --------------------------------------------------------------------------------------
# 2. Interface (fake parser, all six fixtures)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_id", _ALL_FIXTURE_IDS)
async def test_fake_parser_satisfies_contract(fixtures_dir: Path, fixture_id: str) -> None:
    fixture = next(f for f in load_fixtures(fixtures_dir) if f.fixture_id == fixture_id)
    parser = FakeDocumentParser(_canned_elements(fixture))
    result = await check_document_parser_contract(parser, fixture)
    assert result.mime_type == fixture.mime_type


# --------------------------------------------------------------------------------------
# 3. Real parsers (M2-001a turns this green without touching the harness)
# --------------------------------------------------------------------------------------


async def test_real_parsers_parse_all_six_fixtures_when_implemented(
    fixtures_dir: Path,
) -> None:
    entries = {entry["id"]: entry for entry in load_manifest(fixtures_dir)["fixtures"]}
    missing = sorted(
        {
            entry["parser_module"]
            for entry in entries.values()
            if _module_spec(entry["parser_module"]) is None
        }
    )
    if missing:
        pytest.skip(
            "PD-001 parser adapters not implemented yet (M2-001a): "
            f"{', '.join(missing)} — structure + fake-interface contracts passed "
            "for all six fixtures"
        )
    for fixture in load_fixtures(fixtures_dir):
        module_path = entries[fixture.fixture_id]["parser_module"]
        await check_document_parser_contract(_real_parser(module_path), fixture)
