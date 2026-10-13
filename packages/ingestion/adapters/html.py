"""HTML adapter — beautifulsoup4 + lxml (PD-001, MIT).

Strips boilerplate (nav/footer/header/script/style/aside) before normalizing the
article body; h1 -> TITLE, lower headings -> HEADING (section), p -> TEXT,
li -> LIST, table -> TABLE.
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from ..interfaces import DocumentElement, DocumentParser, ElementType, ParsedDocument
from ..normalize import ParseBudgetExceeded, ParseError, clean_text, make_element, validate_input

SUPPORTED_MIME_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_BOILERPLATE_TAGS = ("nav", "footer", "header", "script", "style", "noscript", "aside")


class HtmlParser(DocumentParser):
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
            soup = BeautifulSoup(data, "lxml")
            for tag in soup(_BOILERPLATE_TAGS):
                tag.decompose()
            root = soup.find("article") or soup.body or soup
            elements = _body_elements(root)
            language = _document_language(soup)
        except (ParseError, ParseBudgetExceeded):
            raise
        except Exception as exc:
            raise ParseError(f"failed to parse HTML: {exc}") from exc

        if not elements:
            raise ParseError("HTML produced no extractable content")
        return ParsedDocument(
            elements=elements,
            mime_type=mime_type,
            language=language,
            metadata=dict(metadata or {}),
        )


def _document_language(soup: BeautifulSoup) -> str | None:
    if soup.html is not None:
        value = soup.html.get("lang")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _body_elements(root: Any) -> list[DocumentElement]:
    elements: list[DocumentElement] = []
    section: str | None = None
    for element in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "table"]):
        name = element.name
        if name == "table":
            rows = [
                [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
                for row in element.find_all("tr")
            ]
            table_text = " | ".join(" ".join(cell for cell in row if cell) for row in rows if row)
            if table_text:
                elements.append(make_element(ElementType.TABLE, table_text, section=section))
            continue

        text = clean_text(element.get_text(" ", strip=True))
        if not text:
            continue
        if name == "li":
            elements.append(make_element(ElementType.LIST, text, section=section))
        elif name == "h1":
            elements.append(make_element(ElementType.TITLE, text))
        elif name in {"h2", "h3", "h4"}:
            section = text
            elements.append(make_element(ElementType.HEADING, text, section=section))
        else:
            elements.append(make_element(ElementType.TEXT, text, section=section))
    return elements


PARSER = HtmlParser()


def create_parser() -> DocumentParser:
    return HtmlParser()
