"""Unit tests for PD-008 declared-vs-detected language policy."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("APP_ENV", "test")

from ingestion.interfaces import DocumentElement, ElementType, ParsedDocument
from ingestion.metadata import (
    extract_metadata,
    language_provenance,
    tag_language,
)

pytestmark = pytest.mark.unit


def test_supported_declaration_wins() -> None:
    parsed = ParsedDocument(
        elements=[DocumentElement(type=ElementType.TEXT, text="This is english text.")],
        language="ro",
    )
    assert tag_language(parsed) == "ro"
    prov = language_provenance(parsed)
    assert prov["declared"] == "ro"
    assert prov["conflict"] is False


def test_unsupported_declaration_is_not_overwritten() -> None:
    parsed = ParsedDocument(
        elements=[
            DocumentElement(type=ElementType.TEXT, text="This is english text about retrieval.")
        ],
        language="fr",
    )
    assert tag_language(parsed) == "fr"
    prov = language_provenance(parsed)
    assert prov["declared"] == "fr"
    assert prov["detected"] == "en"
    assert prov["conflict"] is True
    meta = extract_metadata(parsed)
    assert meta.language == "fr"
    assert meta.extra["conflict"] is True
    assert meta.extra["detected"] == "en"


def test_no_declaration_uses_detection() -> None:
    parsed = ParsedDocument(
        elements=[
            DocumentElement(type=ElementType.TEXT, text="This is english text about retrieval.")
        ],
        language=None,
    )
    assert tag_language(parsed) == "en"
    prov = language_provenance(parsed)
    assert prov["declared"] is None
    assert prov["conflict"] is False


def test_unknown_declaration_retained() -> None:
    parsed = ParsedDocument(
        elements=[DocumentElement(type=ElementType.TEXT, text="   ")],
        language="xx",
    )
    assert tag_language(parsed) == "xx"
    prov = language_provenance(parsed)
    assert prov["conflict"] is False
