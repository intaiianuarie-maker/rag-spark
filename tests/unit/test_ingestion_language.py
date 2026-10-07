"""Unit tests for metadata extraction + RO/PT/EN language tagging (M2-001b)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

# Loud precondition (authorized micro, boss 2026-10-07): language tagging needs the
# lingua pin from apps/api/requirements.txt (M2-001b). Without it `detect_language`
# returns None for non-diacritic inputs and these tests fail with a confusing
# `assert None == 'pt'` — a missing dependency must SKIP with the reason instead.
# (This exact artifact was A/B-proved in handoffs/M2-001b-test1.md.)
pytest.importorskip(
    "lingua",
    reason=(
        "lingua-language-detector==2.2.0 is required by the M2-001b language tests "
        "(pin in apps/api/requirements.txt) — install with `pip install -r requirements-dev.txt`"
    ),
)

from ingestion.adapters.txt import PARSER as TXT
from ingestion.chunk import chunk_document
from ingestion.interfaces import DocumentElement, ElementType, ParsedDocument
from ingestion.metadata import detect_language, extract_metadata, tag_language

pytestmark = pytest.mark.unit


def test_detect_language_on_fixture_sentences() -> None:
    assert (
        detect_language("Acesta este un paragraf în limba română despre instrumente de căutare.")
        == "ro"
    )
    assert (
        detect_language("Este é um parágrafo em português sobre recuperação de informação.")
        == "pt"
    )
    assert detect_language(
        "This article body must survive boilerplate stripping for hybrid retrieval."
    ) == "en"


def test_detect_language_short_text_edge_cases() -> None:
    assert detect_language("Bună ziua") == "ro"
    assert detect_language("Olá") == "pt"
    assert detect_language("Hello") == "en"
    assert detect_language("   ") is None


async def test_txt_fixtures_are_tagged_correctly(fixtures_dir: Path) -> None:
    for name, expected in (("txt_ro.txt", "ro"), ("txt_pt.txt", "pt"), ("txt_en.txt", "en")):
        data = (fixtures_dir / "pd001" / name).read_bytes()
        parsed = await TXT.parse(data, mime_type="text/plain")
        assert tag_language(parsed) == expected, name


async def test_chunked_txt_fixture_carries_language(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "pd001" / "txt_ro.txt").read_bytes()
    parsed = await TXT.parse(data, mime_type="text/plain")
    result = chunk_document(parsed, filename="txt_ro.txt")
    assert result.language == "ro"
    assert result.chunks
    assert result.metadata.title


def test_extract_metadata_title_sections_and_counts() -> None:
    parsed = ParsedDocument(
        elements=[
            DocumentElement(type=ElementType.TITLE, text="T"),
            DocumentElement(type=ElementType.HEADING, text="H", section="H"),
            DocumentElement(type=ElementType.TEXT, text="body", section="H"),
        ],
        mime_type="text/markdown",
        page_count=1,
    )
    metadata = extract_metadata(parsed, filename="f.md")
    assert metadata.title == "T"
    assert metadata.sections == ["H"]
    assert metadata.element_count == 3
    assert metadata.mime_type == "text/markdown"
    assert metadata.filename == "f.md"
    assert metadata.language in {"ro", "pt", "en", "unknown"}
