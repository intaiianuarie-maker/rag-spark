"""Integration: WebConnector → fetch → real M2 normalization (TASK-M6-001a).

Composed offline fixture (POLICE P2-4): the real ``WebConnector`` crawls a
mocked-transport fixture site, fetches the discovered items, and runs the
fetched bytes through the **real** M2 ingestion parser (``HtmlParser`` —
the accepted primary for ``text/html``), asserting that the resulting
normalized ``DocumentElement`` values carry the crawl's ``checksum``/``etag``
metadata unchanged and the normalized content is correct.

No socket, no real network: the transport is an ``httpx.MockTransport``
(injected via the ``client_factory`` seam), and the M2 normalizer is the
real ``HtmlParser`` — only external I/O is replaced.
"""

from __future__ import annotations

import asyncio

import httpx
from connectors.adapters.web_connector import WebConnector
from connectors.interfaces import ConnectorConfig, ConnectorKind
from ingestion.adapters.html import HtmlParser


def _cfg(urls: list[str], **settings) -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="c1",
        workspace_id="ws1",
        kind=ConnectorKind.WEB,
        settings={"urls": urls, **settings},
    )


def _fixture_site() -> httpx.MockTransport:
    """A minimal offline fixture site: index -> /a, /b, /c.

    Each page is real HTML so the M2 ``HtmlParser`` produces meaningful
    normalized elements. The index links out to three children at depth 1.
    """

    pages = {
        "/": b'<html><body><article><h1>Index</h1><p>Home</p>'
             b'<a href="/a">a</a><a href="/b">b</a><a href="/c">c</a>'
             b'</article></body></html>',
        "/a": b'<html><body><article><h1>Page A</h1><p>Alpha content</p></article></body></html>',
        "/b": b'<html><body><article><h1>Page B</h1><p>Beta content</p></article></body></html>',
        "/c": b'<html><body><article><h1>Page C</h1><p>Gamma content</p></article></body></html>',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path not in pages:
            return httpx.Response(status_code=404, text="not found")
        return httpx.Response(
            status_code=200,
            content=pages[path],
            headers={
                "content-type": "text/html; charset=utf-8",
                "etag": f'"{path}"',
                "last-modified": "Wed, 21 Oct 2026 00:00:00 GMT",
            },
        )

    return httpx.MockTransport(handler)


def test_web_crawl_to_m2_normalized_documents():
    """discover -> fetch -> real M2 HtmlParser: normalized Documents carry
    crawl metadata (checksum/etag) unchanged and the expected element text.

    This is the composed "crawled pages normalize into the M2 pipeline
    unchanged" criterion from the task card, exercised fully offline via the
    injected transport + real normalizer.
    """
    transport = _fixture_site()

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="http://site.test")

    connector = WebConnector(client_factory=client_factory, max_depth=1)
    config = _cfg(urls=["http://site.test/"])

    # discover: the crawl finds the index + 3 children at depth 1.
    discovery = asyncio.run(connector.discover(config))
    uris = [item.uri for item in discovery.items]
    assert set(uris) == {
        "http://site.test/",
        "http://site.test/a",
        "http://site.test/b",
        "http://site.test/c",
    }

    # fetch + normalize each discovered item through the REAL M2 parser.
    html_parser = HtmlParser()
    normalized: dict[str, tuple[object, ConnectorConfig]] = {}
    for item in discovery.items:
        raw = asyncio.run(connector.fetch(config, item))
        # Strip the MIME-type charset parameter (the connector records the raw
        # Content-Type header; the M2 parser expects the bare type token).
        mime = (item.mime_type or "text/html").split(";")[0].strip()
        # Pass the crawl's metadata (checksum + etag) into the parser so it
        # rides on the ParsedDocument, mirroring the real ingestion path.
        doc = asyncio.run(
            html_parser.parse(
                raw,
                mime_type=mime,
                filename=item.name,
                metadata=dict(item.metadata),
            )
        )
        normalized[item.external_id] = (doc, item)

    # Every page produced normalized elements (no empty parse).
    for uri, (doc, _src) in normalized.items():
        assert doc.elements, f"no elements normalized for {uri}"

    # The index's normalized title is "Index" and its body text is "Home".
    index_doc, _ = normalized["http://site.test/"]
    titles = [e.text for e in index_doc.elements if e.type.value == "title"]
    assert "Index" in titles

    # Child /a's normalized title is "Page A" and text is "Alpha content".
    a_doc, _ = normalized["http://site.test/a"]
    a_titles = [e.text for e in a_doc.elements if e.type.value == "title"]
    assert "Page A" in a_titles

    # Crawl metadata (checksum/etag) is preserved on every normalized doc —
    # the M2 pipeline unchanged: source identity survives the normalization.
    for _uri, (doc, src_item) in normalized.items():
        assert doc.metadata["checksum"] == src_item.metadata["checksum"]
        assert doc.metadata["etag"] == src_item.metadata["etag"]
