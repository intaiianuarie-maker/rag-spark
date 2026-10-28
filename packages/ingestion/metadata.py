"""Deterministic document metadata + language tagging (M2-001b).

Language detection reuses ``lingua-language-detector`` (PD-001 select),
restricted to RO/PT/EN to keep the footprint small. Very short inputs use a
diacritic/stopword heuristic first, then fall back to lingua. No LLM inference,
no network, no randomness.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .interfaces import ElementType, ParsedDocument

SUPPORTED_LANGUAGES = ("ro", "pt", "en")

# lingua is an ingestion runtime dependency (PD-001); guard the import so a
# minimal environment can still import this module.
_DETECTOR: Any = None
_LINGUA_CODES: dict[Any, str] = {}
try:
    from lingua import Language, LanguageDetectorBuilder

    _DETECTOR = LanguageDetectorBuilder.from_languages(
        Language.ROMANIAN, Language.PORTUGUESE, Language.ENGLISH
    ).build()
    _LINGUA_CODES = {
        Language.ROMANIAN: "ro",
        Language.PORTUGUESE: "pt",
        Language.ENGLISH: "en",
    }
except Exception:  # pragma: no cover - import guard for minimal environments
    pass

_RO_DIACRITICS = set("ăâîșțşţĂÂÎȘȚ")
_PT_DIACRITICS = set("ãõçÃÕÇ")
_RO_STOPWORDS = frozenset(
    {"și", "este", "sunt", "care", "pentru", "despre", "acesta", "aceasta", "din", "cu", "la"}
)
_PT_STOPWORDS = frozenset(
    {"que", "não", "uma", "com", "para", "este", "são", "sobre", "do", "da", "recuperação"}
)
_EN_STOPWORDS = frozenset(
    {"the", "and", "is", "of", "to", "in", "this", "that", "with", "for", "about"}
)


class DocumentMetadata(BaseModel):
    """Deterministic metadata extracted from a parsed document."""

    title: str | None = None
    language: str = "unknown"
    mime_type: str | None = None
    page_count: int | None = None
    element_count: int = 0
    sections: list[str] = Field(default_factory=list)
    filename: str | None = None
    extra: dict[str, object] = Field(default_factory=dict)


def _diacritic_hint(text: str) -> str | None:
    if set(text) & _RO_DIACRITICS:
        return "ro"
    if set(text) & _PT_DIACRITICS:
        return "pt"
    return None


def _stopword_hint(text: str) -> str | None:
    words = {word.strip(".,;:!?()[]\"'").lower() for word in text.split()}
    scores = {
        "ro": len(words & _RO_STOPWORDS),
        "pt": len(words & _PT_STOPWORDS),
        "en": len(words & _EN_STOPWORDS),
    }
    best = max(scores, key=lambda code: scores[code])
    return best if scores[best] > 0 else None


def _lingua_code(text: str) -> str | None:
    if _DETECTOR is None:
        return None
    detected = _DETECTOR.detect_language_of(text)
    if detected is None:
        return None
    return _LINGUA_CODES.get(detected)


def detect_language(text: str) -> str | None:
    """Detect RO/PT/EN; ``None`` when the text carries no signal."""
    normalized = text.strip()
    if not normalized:
        return None
    if len(normalized.split()) < 4:
        # Short text: diacritics are decisive; otherwise let lingua try, then stopwords.
        return _diacritic_hint(normalized) or _lingua_code(normalized) or _stopword_hint(normalized)
    return _lingua_code(normalized) or _diacritic_hint(normalized) or _stopword_hint(normalized)


def tag_language(parsed: ParsedDocument) -> str:
    """Language tag for a parsed document; every document gets a value.

    Declared-versus-detected policy (PD-008, consistent):
    - A declared language ALWAYS wins, including unsupported declarations
      (e.g. ``fr``, ``xx``). Detection never silently overwrites a declared
      value, so provenance is preserved and conflicts stay observable.
    - Detection is used only when no language is declared.
    - When a declared (unsupported) language conflicts with a detected
      supported language, the conflict is retained by the caller via
      :func:`language_provenance` rather than resolved by overwriting.
    """
    declared = (parsed.language or "").strip().lower()
    if declared:
        return declared
    combined = " ".join(element.text for element in parsed.elements)
    detected = detect_language(combined)
    if detected:
        return detected
    return "unknown"


def language_provenance(parsed: ParsedDocument) -> dict[str, object]:
    """Return declared/detected language plus a conflict flag (PD-008).

    ``conflict`` is True only when a declared *unsupported* language disagrees
    with a detected supported language. Supported declarations never conflict
    (they are authoritative).
    """
    declared = (parsed.language or "").strip().lower() or None
    combined = " ".join(element.text for element in parsed.elements)
    detected = detect_language(combined)
    conflict = bool(
        declared
        and declared not in SUPPORTED_LANGUAGES
        and detected is not None
        and detected != declared
    )
    return {
        "declared": declared,
        "detected": detected,
        "supported_declared": declared in SUPPORTED_LANGUAGES if declared else False,
        "conflict": conflict,
    }


def extract_metadata(parsed: ParsedDocument, *, filename: str | None = None) -> DocumentMetadata:
    """Extract deterministic metadata from a normalized document."""
    title: str | None = None
    sections: list[str] = []
    for element in parsed.elements:
        if title is None and element.type == ElementType.TITLE and element.text:
            title = element.text
        if element.section and element.section not in sections:
            sections.append(element.section)
    if title is None and sections:
        title = sections[0]
    if title is None and filename:
        title = filename
    extra = dict(parsed.metadata)
    extra.update(language_provenance(parsed))
    extra["language_tag"] = tag_language(parsed)
    return DocumentMetadata(
        title=title,
        language=tag_language(parsed),
        mime_type=parsed.mime_type,
        page_count=parsed.page_count,
        element_count=len(parsed.elements),
        sections=sections,
        filename=filename,
        extra=extra,
    )
