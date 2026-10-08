"""Answer engine data models (TASK-M4-002).

The structured LLM output is ``{answer, claims[{text, chunk_ids}]}``; the
verified, citation-ready output is :class:`Answer`. Citations carry the
provenance the UI needs to open the exact passage (doc, filename, page,
section, span).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Claim(BaseModel):
    """A grounded statement the model ties to evidence chunk ids."""

    text: str
    chunk_ids: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    """A verified pointer to a real, in-scope chunk (never fabricated)."""

    chunk_id: str
    document_id: str | None = None
    filename: str | None = None
    page: int | None = None
    section: str | None = None
    #: Character offsets ``(start, end)`` of the claim text inside the chunk,
    #: or ``None`` when the span could not be located.
    span: tuple[int, int] | None = None
    workspace_id: str | None = None


class Answer(BaseModel):
    """Final answer: text + verified claims + citations."""

    answer: str
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
