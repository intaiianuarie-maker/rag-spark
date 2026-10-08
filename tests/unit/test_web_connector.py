"""Unit tests for the WebConnector adapter (TASK-M6-001).

Deterministic crawl semantics with fakes: seed extraction, config caps,
breadth-first link discovery (bounded by depth), cursor paging (stable order),
dead-link tolerance, and checksum/etag metadata — all without network.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from connectors.adapters.web_connector import WebConnector
from connectors.interfaces import ConnectorConfig, ConnectorKind, ConnectorSyncResult


def _cfg(urls: list[str] | None = None, **settings: Any) -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="c1",
        workspace_id="ws1",
        kind=ConnectorKind.WEB,
        settings={"urls": urls or [], **settings},
    )


class FakeFetcher:
    """Programmable transport: url -> (content, headers)."""

    def __init__(self, pages: dict[str, tuple[bytes, dict[str, str]]]) -> None:
        self.pages = pages

    async def __call__(self, url: str) -> tuple[bytes, dict[str, str]]:
        if url not in self.pages:
            raise ConnectionError(f"404: {url}")
        return self.pages[url]


def test_seeds_from_settings_urls():
    c = WebConnector(fetch=FakeFetcher({}))
    assert c._seeds(_cfg(urls=["http://a.test/", "http://b.test/"])) == [
        "http://a.test/",
        "http://b.test/",
    ]


def test_seeds_require_urls():
    import pytest

    c = WebConnector(fetch=FakeFetcher({}))
    with pytest.raises(ValueError, match="urls"):
        c._seeds(_cfg())


def test_constructor_validates_caps():
    import pytest

    with pytest.raises(ValueError):
        WebConnector(page_size=0)
    with pytest.raises(ValueError):
        WebConnector(max_depth=-1)


def test_discover_single_page_no_links():
    pages = {"http://x.test/": (b"<html>hello</html>", {})}
    c = WebConnector(fetch=FakeFetcher(pages))
    disc = asyncio.run(c.discover(_cfg(urls=["http://x.test/"])))
    assert [i.uri for i in disc.items] == ["http://x.test/"]
    assert disc.next_cursor is None


def test_crawl_follows_links_depth_bounded():
    pages = {
        "http://x.test/": (b'<a href="/a">a</a>', {}),
        "http://x.test/a": (b'<a href="/b">b</a>', {}),
        "http://x.test/b": (b"leaf", {}),
    }
    c = WebConnector(fetch=FakeFetcher(pages), max_depth=1)
    disc = asyncio.run(c.discover(_cfg(urls=["http://x.test/"])))
    uris = {i.uri for i in disc.items}
    # depth 1: root + one hop, but not the second hop /b
    assert "http://x.test/" in uris and "http://x.test/a" in uris
    assert "http://x.test/b" not in uris


def test_crawl_depth_zero_is_seed_only():
    pages = {
        "http://x.test/": (b'<a href="/a">a</a>', {}),
        "http://x.test/a": (b"a", {}),
    }
    c = WebConnector(fetch=FakeFetcher(pages), max_depth=0)
    disc = asyncio.run(c.discover(_cfg(urls=["http://x.test/"])))
    assert {i.uri for i in disc.items} == {"http://x.test/"}


def test_cursor_paging_is_stable_and_complete():
    pages = {
        "http://x.test/": (b'<a href="/a">a</a><a href="/b">b</a><a href="/c">c</a>', {}),
        "http://x.test/a": (b"a", {}),
        "http://x.test/b": (b"b", {}),
        "http://x.test/c": (b"c", {}),
    }
    c = WebConnector(fetch=FakeFetcher(pages), page_size=2, max_depth=1)
    cfg = _cfg(urls=["http://x.test/"])
    d1 = asyncio.run(c.discover(cfg))
    assert d1.next_cursor == "2"
    d2 = asyncio.run(c.discover(cfg, cursor=d1.next_cursor))
    assert d2.next_cursor is None
    # union of both pages == full crawl, no dupes
    seen = {i.uri for i in d1.items} | {i.uri for i in d2.items}
    assert seen == {
        "http://x.test/",
        "http://x.test/a",
        "http://x.test/b",
        "http://x.test/c",
    }


def test_dead_link_is_skipped_not_fatal():
    pages = {"http://x.test/": (b'<a href="/dead">d</a>', {})}
    c = WebConnector(fetch=FakeFetcher(pages))
    disc = asyncio.run(c.discover(_cfg(urls=["http://x.test/"])))
    uris = {i.uri for i in disc.items}
    assert "http://x.test/" in uris
    assert "http://x.test/dead" not in uris  # 404 link dropped


def test_metadata_carries_checksum_and_etag():
    body = b"<html>content</html>"
    pages = {"http://x.test/": (body, {"etag": "abc123"})}
    c = WebConnector(fetch=FakeFetcher(pages))
    disc = asyncio.run(c.discover(_cfg(urls=["http://x.test/"])))
    item = disc.items[0]
    assert item.metadata["etag"] == "abc123"
    assert item.metadata["checksum"] == hashlib.sha256(body).hexdigest()


def test_sync_aggregates_discovered_and_fetched():
    pages = {
        "http://x.test/": (b'<a href="/a">a</a>', {}),
        "http://x.test/a": (b"a", {}),
    }
    c = WebConnector(fetch=FakeFetcher(pages), max_depth=1)
    result = asyncio.run(c.sync(_cfg(urls=["http://x.test/"])))
    assert isinstance(result, ConnectorSyncResult)
    assert result.discovered >= 1
    assert result.fetched == result.discovered
