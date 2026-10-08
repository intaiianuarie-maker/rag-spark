"""Web connector (TASK-M6-001) — crawl pages over HTTP via httpx.

Discovers web pages from seed URLs, pages results with a cursor that never
re-serves an item, and fetches raw bytes (HTML/PDF/etc.). No vendor SDK: httpx
is already a product dependency and stays inside the adapter.

The HTTP fetch is an injectable seam (``fetch`` callable returning bytes) so the
connector is fully testable offline against the shared contract without network
access. The crawl is bounded by ``max_depth``/``max_pages`` so a run is
deterministic and cannot walk the open web.

Incremental-sync hooks: ``SourceItem.updated_at`` (from Last-Modified/ETag when
present) and a content checksum in ``metadata["checksum"]`` let ingestion dedup
(M2-001d) decide new vs changed. Cursor paging guarantees a resumable scan.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from ..interfaces import (
    Connector,
    ConnectorConfig,
    ConnectorDiscovery,
    ConnectorKind,
    ConnectorSyncResult,
    SourceItem,
)

#: A fetch seam: given a URL, return (bytes, headers). Injectable for tests.
FetchFn = Callable[[str], Awaitable[tuple[bytes, dict[str, str]]]]


def _default_headers(resp: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in dict(resp.headers).items()}


class WebConnector(Connector):
    """Crawl a bounded set of same-origin web pages."""

    kind = ConnectorKind.WEB

    def __init__(
        self,
        page_size: int = 50,
        max_pages: int = 500,
        max_depth: int = 1,
        fetch: FetchFn | None = None,
    ) -> None:
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        if max_pages < 1:
            raise ValueError(f"max_pages must be >= 1, got {max_pages}")
        if max_depth < 0:
            raise ValueError(f"max_depth must be >= 0, got {max_depth}")
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_depth = max_depth
        self._fetch = fetch

    def _seeds(self, config: ConnectorConfig) -> list[str]:
        raw = config.settings.get("urls")
        if not raw:
            raise ValueError("WebConnector requires settings.urls (list of seed URLs)")
        return [str(u) for u in raw]

    async def _fetch_bytes(self, url: str) -> tuple[bytes, dict[str, str]]:
        if self._fetch is not None:
            return await self._fetch(url)
        import httpx

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content, _default_headers(resp)

    def _discover_links(self, base: str, html: bytes) -> list[str]:
        """Extract same-origin links from an HTML page (best-effort, stdlib re)."""
        import re

        parsed = urlparse(base)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        links: list[str] = []
        for match in re.finditer(rb'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
            try:
                href = match.group(1).decode("utf-8", "ignore")
            except Exception:
                continue
            absolute = urljoin(base, href)
            if absolute.startswith(origin) and absolute not in links:
                links.append(absolute)
        return links

    async def _crawl(self, config: ConnectorConfig) -> list[SourceItem]:
        """Breadth-first crawl from the seeds, bounded by depth/page caps.

        Returns items in discovery order (stable given the same inputs); the
        cursor then pages this deterministic list.
        """
        seeds = self._seeds(config)
        seen: set[str] = set()
        items: list[SourceItem] = []
        frontier: list[tuple[str, int]] = [(u, 0) for u in seeds]
        while frontier and len(items) < self._max_pages:
            url, depth = frontier.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                content, headers = await self._fetch_bytes(url)
            except Exception:
                # A dead link (404/timeout/DNS) is silently dropped from
                # discovery: ingestion only sees pages that returned content,
                # so a vanished URL never pollutes counts or triggers a
                # tombstone against an unrelated record.
                continue
            parsed = urlparse(url)
            last_mod = headers.get("last-modified")
            etag = headers.get("etag")
            updated = None
            if last_mod:
                try:
                    updated = parsedate_to_datetime(last_mod)
                except Exception:
                    updated = None
            items.append(
                SourceItem(
                    external_id=url,
                    name=parsed.path.rsplit("/", 1)[-1] or parsed.netloc,
                    uri=url,
                    mime_type=headers.get("content-type"),
                    size_bytes=len(content),
                    updated_at=updated,
                    metadata={
                        "checksum": hashlib.sha256(content).hexdigest(),
                        **({"etag": etag} if etag else {}),
                    },
                )
            )
            if depth < self._max_depth:
                for link in self._discover_links(url, content):
                    if link not in seen:
                        frontier.append((link, depth + 1))
        return items

    async def discover(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorDiscovery:
        all_items = await self._crawl(config)
        start = int(cursor) if cursor else 0
        window = all_items[start : start + self._page_size]
        next_start = start + self._page_size
        next_cursor = str(next_start) if next_start < len(all_items) else None
        return ConnectorDiscovery(items=window, next_cursor=next_cursor)

    async def fetch(self, config: ConnectorConfig, item: SourceItem) -> bytes:
        content, _headers = await self._fetch_bytes(item.uri or item.external_id)
        return content

    async def sync(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorSyncResult:
        discovery = await self.discover(config, cursor)
        fetched = 0
        errors: list[str] = []
        for item in discovery.items:
            if item.metadata.get("error"):
                errors.append(f"{item.external_id}: {item.metadata['error']}")
                continue
            try:
                await self.fetch(config, item)
                fetched += 1
            except Exception as exc:
                errors.append(f"{item.external_id}: {exc}")
        return ConnectorSyncResult(
            discovered=len(discovery.items),
            fetched=fetched,
            next_cursor=discovery.next_cursor,
            errors=errors,
        )
