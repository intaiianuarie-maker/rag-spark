"""Markdown adapter — markdown-it-py (PD-001, MIT).

Preserves heading structure (section tracking), list items, and fenced code as a
CODE element.
"""

from __future__ import annotations

from typing import Any

from markdown_it import MarkdownIt

from ..interfaces import DocumentElement, DocumentParser, ElementType, ParsedDocument
from ..normalize import ParseError, clean_text, make_element, validate_input

SUPPORTED_MIME_TYPES = frozenset({"text/markdown", "text/x-markdown"})
_parser = MarkdownIt("commonmark")


class MarkdownParser(DocumentParser):
    supported_mime_types = SUPPORTED_MIME_TYPES

    async def parse(
        self,
        data: bytes,
        *,
        mime_type: str,
        filename: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ParsedDocument:
        validate_input(data, mime_type, SUPPORTED_MIME_TYPES)
        try:
            text = data.decode("utf-8")
            elements = _elements(_parser.parse(text))
        except UnicodeDecodeError as exc:
            raise ParseError(f"markdown is not valid UTF-8: {exc}") from exc
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"failed to parse markdown: {exc}") from exc

        if not elements:
            raise ParseError("markdown produced no extractable content")
        return ParsedDocument(elements=elements, mime_type=mime_type, metadata=dict(metadata or {}))


def _elements(tokens: list[Any]) -> list[DocumentElement]:
    elements: list[DocumentElement] = []
    section: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "heading_open":
            text = clean_text(_inline_content(tokens, index))
            if token.tag == "h1":
                elements.append(make_element(ElementType.TITLE, text))
            elif text:
                section = text
                elements.append(make_element(ElementType.HEADING, text, section=section))
            index += 2
            continue
        if token.type == "paragraph_open":
            text = clean_text(_inline_content(tokens, index))
            if text:
                elements.append(make_element(ElementType.TEXT, text, section=section))
            index += 2
            continue
        if token.type == "list_item_open":
            cursor = index + 1
            while cursor < len(tokens) and tokens[cursor].type != "list_item_close":
                if tokens[cursor].type == "inline":
                    text = clean_text(tokens[cursor].content)
                    if text:
                        elements.append(make_element(ElementType.LIST, text, section=section))
                    break
                cursor += 1
            index = cursor + 1
            continue
        if token.type == "fence":
            content = token.content.strip()
            if content:
                elements.append(make_element(ElementType.CODE, content, section=section))
        index += 1
    return elements


def _inline_content(tokens: list[Any], index: int) -> str:
    if index + 1 < len(tokens) and tokens[index + 1].type == "inline":
        return str(tokens[index + 1].content)
    return ""


PARSER = MarkdownParser()


def create_parser() -> DocumentParser:
    return MarkdownParser()
