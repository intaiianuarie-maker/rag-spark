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


class FakeClock:
    """Recording politeness-delay clock: no real sleep, deterministic.

    Mirrors the injectable ``sleep`` seam so unit tests assert on the delay
    values the crawl requested instead of sleeping on wall-clock time. Also
    keeps an ``events`` log (``fetch``/``sleep`` tokens, in order) when the
    fetch seam is wired to it, which pins the *ordering* of pacing relative to
    fetches (a sleep must come before each fetch after the first).
    """

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.events: list[str] = []

    def record_fetch(self, url: str) -> None:
        self.events.append(f"fetch:{url}")

    def record_sleep(self) -> None:
        self.events.append("sleep")

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.record_sleep()


def test_crawl_pacing_order_is_fetch_sleep_fetch():
    """Finding 1 guard: the delay is paced *before* each fetch after the first.

    A 3-page crawl must emit the event sequence
    ``fetch -> sleep -> fetch -> sleep -> fetch`` (a gap between every
    consecutive pair, no leading sleep before the very first fetch, and no
    trailing sleep after the last). An unpaced opening (two back-to-back
    fetches) or a useless trailing sleep would both fail this assertion.
    """
    pages = {
        "http://x.test/": (b'<a href="/a">a</a>', {}),
        "http://x.test/a": (b'<a href="/b">b</a>', {}),
        "http://x.test/b": (b"leaf", {}),
    }
    clock = FakeClock()

    async def tracking_fetch(url: str) -> tuple[bytes, dict[str, str]]:
        clock.record_fetch(url)
        return pages[url]

    c = WebConnector(
        fetch=tracking_fetch,
        max_depth=2,
        politeness_delay_seconds=0.5,
        sleep=clock,
    )
    disc = asyncio.run(c.discover(_cfg(urls=["http://x.test/"])))
    assert [i.uri for i in disc.items] == [
        "http://x.test/",
        "http://x.test/a",
        "http://x.test/b",
    ]
    # Interleave of fetch/sleep events: fetch, sleep, fetch, sleep, fetch.
    assert clock.events == [
        "fetch:http://x.test/",
        "sleep",
        "fetch:http://x.test/a",
        "sleep",
        "fetch:http://x.test/b",
    ]


def test_crawl_honors_politeness_delay_between_fetches():
    """A 3-page crawl with a 0.5s delay sleeps before every fetch after the first.

    With 3 fetched pages the clock records exactly 2 gaps (one before each
    fetch except the first), each equal to the configured delay. This is the
    core "politeness delay honored" criterion: the delay is applied between
    consecutive fetches and scales with the page count.
    """
    pages = {
        "http://x.test/": (b'<a href="/a">a</a>', {}),
        "http://x.test/a": (b"<html>a</html>", {}),
    }
    clock = FakeClock()
    c = WebConnector(
        fetch=FakeFetcher(pages),
        max_depth=1,
        politeness_delay_seconds=0.5,
        sleep=clock,
    )
    disc = asyncio.run(c.discover(_cfg(urls=["http://x.test/"])))
    assert [i.uri for i in disc.items] == ["http://x.test/", "http://x.test/a"]
    # 2 fetched pages -> 1 inter-fetch gap of 0.5s.
    assert clock.delays == [0.5]


def test_crawl_delay_is_global_inter_request_gap():
    """Delay count scales with the number of *fetched* pages, not links.

    A 4-page crawl (root + 3 children) with depth 2 records 3 gaps — one
    before each of the 2nd/3rd/4th successful fetch. Failed fetches (dead
    links) do not count toward the gap because the delay is applied before a
    fetch only when a page has already been fetched.
    """
    pages = {
        "http://x.test/": (b'<a href="/a"><a href="/b"><a href="/c">', {}),
        "http://x.test/a": (b"a", {}),
        "http://x.test/b": (b"b", {}),
        "http://x.test/c": (b"c", {}),
    }
    clock = FakeClock()
    c = WebConnector(
        fetch=FakeFetcher(pages),
        max_depth=2,
        politeness_delay_seconds=2.0,
        sleep=clock,
    )
    disc = asyncio.run(c.discover(_cfg(urls=["http://x.test/"])))
    # 4 pages fetched -> 3 gaps.
    assert len(disc.items) == 4
    assert clock.delays == [2.0, 2.0, 2.0]


def test_crawl_default_no_delay_records_no_sleep():
    """The default delay is 0s, so no sleep is requested (backward compatible).

    No settings and no explicit constructor delay -> the clock is never
    called, preserving the pre-fix zero-delay behaviour. This is the
    "DEFAULT_NO_DELAY" fixture: it proves the absence of a sleep on the
    default path without asserting on real time.
    """
    pages = {
        "http://x.test/": (b'<a href="/a">a</a>', {}),
        "http://x.test/a": (b"a", {}),
    }
    clock = FakeClock()
    c = WebConnector(fetch=FakeFetcher(pages), max_depth=1, sleep=clock)
    asyncio.run(c.discover(_cfg(urls=["http://x.test/"])))
    assert clock.delays == []  # default delay 0 -> no sleep requested


def test_delay_via_settings_seconds():
    """``settings["politeness_delay"]`` (float seconds) is honored.

    Same inter-fetch-gap semantics as the constructor path; a crawl of 2
    pages records 1 gap equal to the settings value.
    """
    pages = {
        "http://x.test/": (b'<a href="/a">a</a>', {}),
        "http://x.test/a": (b"a", {}),
    }
    clock = FakeClock()
    c = WebConnector(fetch=FakeFetcher(pages), max_depth=1, sleep=clock)
    cfg = _cfg(urls=["http://x.test/"], politeness_delay=1.5)
    asyncio.run(c.discover(cfg))
    assert clock.delays == [1.5]


def test_delay_via_settings_ms():
    """``settings["politeness_delay_ms"]`` (int ms) is converted to seconds.

    500ms -> 0.5s gap between the two fetched pages.
    """
    pages = {
        "http://x.test/": (b'<a href="/a">a</a>', {}),
        "http://x.test/a": (b"a", {}),
    }
    clock = FakeClock()
    c = WebConnector(fetch=FakeFetcher(pages), max_depth=1, sleep=clock)
    cfg = _cfg(urls=["http://x.test/"], politeness_delay_ms=500)
    asyncio.run(c.discover(cfg))
    assert clock.delays == [0.5]


def test_invalid_politeness_delay_raises_value_error():
    """Out-of-range delays are rejected at configuration time (bounded).

    Negative, non-finite, and over-the-max values all raise ``ValueError``
    in the same style as the other bounded settings.
    """
    import pytest

    pages = {"http://x.test/": (b"<html>hi</html>", {})}
    c = WebConnector(fetch=FakeFetcher(pages))
    with pytest.raises(ValueError):
        c._validate_politeness_delay(-1.0)
    with pytest.raises(ValueError):
        c._validate_politeness_delay(float("nan"))
    with pytest.raises(ValueError):
        c._validate_politeness_delay(float("inf"))
    with pytest.raises(ValueError):
        c._validate_politeness_delay(10_000_000.0)  # exceeds the sane bound


def test_constructor_rejects_invalid_delay():
    """A bad constructor delay is validated up front (fail loudly)."""
    import pytest

    with pytest.raises(ValueError):
        WebConnector(politeness_delay_seconds=-1.0)
    with pytest.raises(ValueError):
        WebConnector(fetch=FakeFetcher({}), politeness_delay_seconds=1e9)


def test_sync_honors_delay_between_fetched_items():
    """``sync`` paces its own fetch loop with the same politeness delay.

    ``sync`` = ``discover`` (a crawl of N pages -> N-1 crawl gaps, each before
    a fetch) followed by a re-fetch loop over the discovered items (N-1 more
    gaps). For a 3-item sync with a 0.25s delay the clock records 4 gaps: 2
    from the internal crawl + 2 from the sync re-fetch loop. The point is
    that the sync fetch loop paces before each re-fetch after the first,
    consistent with the crawl.
    """
    pages = {
        "http://x.test/": (b'<a href="/a"><a href="/b">', {}),
        "http://x.test/a": (b"a", {}),
        "http://x.test/b": (b"b", {}),
    }
    clock = FakeClock()
    c = WebConnector(
        fetch=FakeFetcher(pages),
        max_depth=1,
        politeness_delay_seconds=0.25,
        sleep=clock,
    )
    result = asyncio.run(c.sync(_cfg(urls=["http://x.test/"])))
    assert result.fetched == result.discovered == 3
    # 3 pages: crawl records 2 inter-page gaps, then the sync re-fetch loop
    # records 2 more (one after its 2nd and 3rd item).
    assert clock.delays == [0.25, 0.25, 0.25, 0.25]


def test_sync_delay_only_in_fetch_loop_not_double_charged():
    """With zero discovered pages the sync loop records no gap.

    A sync with no crawl pages (empty discovery) has no fetch-loop items to
    pace, so the clock is untouched — no gap is charged for a no-op loop.
    """
    clock = FakeClock()
    # No links: single-page crawl gives 0 crawl gaps; a single re-fetch gives
    # 0 fetch-loop gaps (the first fetch never sleeps before it).
    c = WebConnector(
        fetch=FakeFetcher({"http://x.test/": (b"<html>hi</html>", {})}),
        max_depth=0,
        politeness_delay_seconds=0.25,
        sleep=clock,
    )
    result = asyncio.run(c.sync(_cfg(urls=["http://x.test/"])))
    assert result.fetched == 1
    # Single fetch: the sleep only happens *between* fetches, so a lone fetch
    # never triggers one.
    assert clock.delays == []


def test_crawl_paces_leading_failed_fetches():
    """P2-1: pacing counts ATTEMPTS, not just successes.

    Seeds [bad1, bad2, good] where the injected fetch raises for bad1/bad2
    and succeeds for good: the two leading failed requests are still HTTP
    requests the host sees, so a gap must fire before bad2 and before good.
    Under the old success-only counter a leading failure never incremented it
    (the first request failed, so the counter stayed at 0), and the host was
    hammered by all three requests back-to-back — this fixture is the minimal
    case that discriminates attempt-counting from success-counting.
    """
    pages = {"http://x.test/good": (b"<html>good</html>", {})}

    async def failing_fetch(url: str) -> tuple[bytes, dict[str, str]]:
        if url in pages:
            return pages[url]
        raise ConnectionError(f"404: {url}")

    clock = FakeClock()
    c = WebConnector(
        fetch=failing_fetch,
        politeness_delay_seconds=0.5,
        sleep=clock,
    )
    disc = asyncio.run(
        c.discover(_cfg(urls=["http://x.test/bad1", "http://x.test/bad2", "http://x.test/good"]))
    )
    # Only the good page becomes an item (dead links dropped) ...
    assert [i.uri for i in disc.items] == ["http://x.test/good"]
    # ...but all THREE attempts were paced: a gap before bad2 and before good.
    assert clock.delays == [0.5, 0.5]


def test_sync_paces_leading_failed_refetch():
    """P2-1: sync re-fetch loop counts ATTEMPTS, not just successes.

    Isolated to the loop alone: a two-page crawl where the loop's first
    re-fetch attempt (the single page) fails transiently, so the loop's
    leading attempt errors. The loop records a gap before the NEXT attempt
    even when the leading attempt failed. For a two-page crawl, the crawl
    records 1 gap and the loop records 1 gap (before the 2nd of its 2
    attempts, which is the only one after the failure) -> total of 2 gaps
    for 2 crawl attempts + 2 loop attempts.
    """
    pages = {
        "http://x.test/": (b'<a href="/a">', {}),
        "http://x.test/a": (b"a", {}),
    }

    class FailFirstLoopAttempt:
        """Succeeds on crawl, fails on the loop's first re-fetch, then succeeds.

        The crawl needs calls 1 (root) + 2 (/a) to succeed; the loop re-fetches
        each item in order, so the loop's first attempt is call 3 (root) which
        fails, and the loop's second attempt is call 4 (/a) which succeeds.
        """

        def __init__(self, pages) -> None:
            self.pages = pages
            self.call_no = 0

        async def __call__(self, url: str) -> tuple[bytes, dict[str, str]]:
            self.call_no += 1
            # Calls 1-2 are the crawl (root + /a) — both succeed.
            # Call 3 is the loop's first re-fetch (root) — fail it.
            # Call 4 is the loop's second re-fetch (/a) — succeed.
            if self.call_no == 3:
                raise ConnectionError("loop's leading re-fetch failed")
            if url not in self.pages:
                raise ConnectionError(f"404: {url}")
            return self.pages[url]

    clock = FakeClock()
    c = WebConnector(
        fetch=FailFirstLoopAttempt(pages),
        max_depth=1,
        politeness_delay_seconds=0.25,
        sleep=clock,
    )
    result = asyncio.run(c.sync(_cfg(urls=["http://x.test/"])))
    # 2 items discovered; the loop's leading attempt failed -> 1 of 2 fetched.
    assert result.discovered == 2
    assert result.fetched == 1
    assert len(result.errors) == 1
    # Crawl: 2 attempts -> 1 gap (before /a).
    # Loop: 2 attempts -> 1 gap (before the /a re-fetch, after the failed root
    # re-fetch). The P2-1 fix is that the loop's leading FAILED attempt still
    # paces the gap before the next attempt — the old success-only counter
    # would have recorded 0 loop gaps.
    assert clock.delays == [0.25, 0.25]


def test_redirect_hops_are_paced_offline():
    """P2-2: redirect hops inside the real httpx branch are paced.

    Drives the actual ``_fetch_bytes`` redirect loop (not the injected fetch
    seam) with an offline ``httpx.MockTransport``: /redirect -> 302 -> /target
    -> 200. The second hop (the /target GET) is paced before it fires.
    """
    import httpx

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        path = request.url.path
        if path == "/redirect":
            return httpx.Response(
                status_code=302, headers={"location": "/target"}, text="redirect"
            )
        return httpx.Response(status_code=200, text="target content")

    transport = httpx.MockTransport(handler)

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="http://x.test")

    clock = FakeClock()
    c = WebConnector(
        client_factory=client_factory,
        politeness_delay_seconds=0.5,
        sleep=clock,
    )
    (body, _headers) = asyncio.run(
        c._fetch_bytes(_cfg(urls=["http://x.test/redirect"]), "http://x.test/redirect")
    )
    # Both hops hit the host: /redirect (302) then /target (200).
    assert calls == ["http://x.test/redirect", "http://x.test/target"]
    assert body == b"target content"
    # One redirect hop after the first request -> one paced gap.
    assert clock.delays == [0.5]


def test_redirect_hop_no_gap_for_single_non_redirect_request():
    """P2-2: a plain single GET (no redirect) records no gap.

    The first request of a fetch never sleeps before it (same rule as the
    crawl), so a /page -> 200 with no redirect records zero delays.
    """
    import httpx

    transport = httpx.MockTransport(lambda req: httpx.Response(status_code=200, text="ok"))

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="http://x.test")

    clock = FakeClock()
    c = WebConnector(client_factory=client_factory, politeness_delay_seconds=0.5, sleep=clock)
    (body, _headers) = asyncio.run(
        c._fetch_bytes(_cfg(urls=["http://x.test/page"]), "http://x.test/page")
    )
    assert body == b"ok"
    assert clock.delays == []
