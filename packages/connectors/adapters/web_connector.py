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

T-V1-034 (redirect / origin policy, pinned in code):
- the crawl is **origin-scoped**: allowed origins are the origins of
  ``settings.urls`` plus an explicit ``settings["allowed_origins"]`` list;
  a link whose origin is not in that set is never fetched (a real
  ``scheme://host[:port]`` comparison, not a string prefix, so
  ``https://seed.test.evil.example`` is out of scope);
- redirects are followed **manually, hop by hop** (``follow_redirects`` is
  off): every ``Location`` is resolved and re-checked against the allowed
  origins, so a 302 cannot walk the crawler off its configured scope;
- seeds must be plain ``http``/``https`` URLs; ``file:``, ``data:`` and
  ``javascript:`` are rejected loudly at configuration time.

Politeness delay (TASK-M6-001a "delay"):
- a bounded politeness delay is honored before each crawl fetch after the
  first (global inter-request gap; there is one crawl worker). It defaults to
  ``0`` (no delay) so the existing deterministic crawl tests stay green and is
  configured via ``settings["politeness_delay"]`` (float seconds) or
  ``settings["politeness_delay_ms"]`` (int ms), or the explicit
  ``WebConnector(politeness_delay_seconds=...)`` constructor parameter; any
  value is validated to a non-negative finite range (``ValueError``) the same
  way the other bounds are. The gap is paced *before* the fetch (once a page
  has already been fetched), so consecutive requests are spaced and no trailing
  sleep is charged after the final page.
- the sleep is an injectable seam (``sleep`` callable) defaulting to a real
  ``asyncio.sleep``; tests inject a recording fake so the delay is verified
  deterministically with no wall-clock sleeping.

Redirect-hop pacing: the real httpx transport follows redirects manually,
hop by hop; every hop after the first is paced by the same delay (a redirect
chain is still N requests to the host). The client can be injected via
``client_factory`` so tests drive the actual redirect-hop branch with an
offline ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import math
import socket
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
    page_items,
    snapshot_digest,
)

logger = logging.getLogger("knowledge_os.connectors.web")

#: A fetch seam: given a URL, return (bytes, headers). Injectable for tests.
FetchFn = Callable[[str], Awaitable[tuple[bytes, dict[str, str]]]]

#: A clock / sleep seam: an async sleep callable taking seconds. ``None``
#: means the default ``asyncio.sleep`` (real wall clock); tests inject a fake
#: that records the requested delay so the delay is verified deterministically
#: with no real sleeping.
SleepFn = Callable[[float], Awaitable[None]]

#: Redirect status codes the manual hop loop understands.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: A transport seam: an optional ``httpx.AsyncClient`` used by the real
#: (non-injected) HTTP path so tests can drive the actual httpx redirect-hop
#: loop with an offline ``MockTransport`` — no socket, no real network.
ClientFactory = Callable[[], Any]

#: Default bound on manual redirect hops per fetch (a redirect loop must not
#: spin the crawler). Override with ``settings["max_redirects"]``.
DEFAULT_MAX_REDIRECTS = 5

#: Politeness delay (TASK-M6-001a "delay"): seconds to wait between two
#: consecutive requests to a host. Default 0 keeps the existing zero-delay
#: behaviour (fast deterministic tests, no wall-clock sleeps) and is overridden
#: per connector via ``settings["politeness_delay"]`` (float seconds) or
#: ``settings["politeness_delay_ms"]`` (int ms). Pacing is a *global*
#: inter-request gap applied BEFORE every request after the first — in the
#: crawl between pages, in the sync re-fetch loop between items, and inside
#: ``_fetch_bytes`` between redirect hops — so a multi-page crawl, a re-fetch
#: loop, and a redirect chain each spread their requests over the configured
#: gap. The model is a single crawl worker pacing its own global request
#: stream, not a per-host minimum interval.
DEFAULT_POLITENESS_DELAY_SECONDS = 0.0

#: A politeness delay must be a non-negative, finite number of seconds; a
#: negative or unbounded value would stall the crawl or busy-loop it.
_MAX_POLITENESS_DELAY_SECONDS = 3600.0


def origin_of(url: str) -> str | None:
    """Return ``scheme://host[:port]`` for a plain http(s) URL, else ``None``.

    Comparison key for the origin policy: scheme and host are case-folded and
    default ports are dropped, so ``https://Seed.TEST:443/`` and
    ``https://seed.test/`` are the same origin. Anything without an
    ``http``/``https`` scheme *and* a host (``file:///etc/passwd``,
    ``javascript:void(0)``, ``data:...``) yields ``None`` = never in scope.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if parsed.username or parsed.password:
        return None  # credentialed URLs are not a plain origin we can pin
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


class OutOfScopeRedirect(ValueError):
    """A redirect (or link) pointed outside the configured origins."""


def resolve_redirect(
    current_url: str,
    location: str,
    allowed_origins: set[str],
    *,
    max_redirects: int,
    hops: int,
) -> str:
    """Validate one redirect hop and return the next URL to fetch.

    Raises :class:`OutOfScopeRedirect` when the hop leaves the allowed origins
    and ``ValueError`` once ``max_redirects`` is exhausted — never silently
    continues to a host the workspace did not configure.
    """
    if not location:
        raise OutOfScopeRedirect(f"redirect from {current_url} has no Location")
    if hops >= max_redirects:
        raise ValueError(f"exceeded {max_redirects} redirects fetching {current_url}")
    next_url = urljoin(current_url, location)
    origin = origin_of(next_url)
    if origin is None or origin not in allowed_origins:
        raise OutOfScopeRedirect(
            f"redirect from {current_url} left the allowed origins (origin={origin!r})"
        )
    return next_url


def _assert_public_host(url: str) -> None:
    """Refuse a fetch whose host resolves to a non-public address (SSRF guard).

    ponytail: resolve-then-fetch. httpx resolves again on connect, so a record that
    rebinds between this check and the connect still gets through. Upgrade: connect
    to the vetted address directly.
    """
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise OutOfScopeRedirect(f"cannot resolve {host!r}: {exc}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not addr.is_global:
            raise OutOfScopeRedirect(f"{host!r} resolves to non-public address {addr}")


def _default_headers(resp: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in dict(resp.headers).items()}


async def _default_sleep(seconds: float) -> None:
    """Default politeness-delay clock: a real wall-clock sleep.

    Inject a fake via ``WebConnector(sleep=...)`` to make unit tests
    deterministic (the fake records the requested delay instead of sleeping).
    """
    await asyncio.sleep(seconds)


class WebConnector(Connector):
    """Crawl a bounded set of same-origin web pages."""

    kind = ConnectorKind.WEB

    def __init__(
        self,
        page_size: int = 50,
        max_pages: int = 500,
        max_depth: int = 1,
        fetch: FetchFn | None = None,
        politeness_delay_seconds: float | None = None,
        sleep: SleepFn | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        if max_pages < 1:
            raise ValueError(f"max_pages must be >= 1, got {max_pages}")
        if max_depth < 0:
            raise ValueError(f"max_depth must be >= 0, got {max_depth}")
        # A politeness-delay given explicitly (not via settings) must already be
        # a valid non-negative number; the settings path is validated separately
        # in ``_resolve_politeness_delay`` against the same bounds.
        if politeness_delay_seconds is not None:
            self._validate_politeness_delay(politeness_delay_seconds)
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_depth = max_depth
        self._fetch = fetch
        # Default to the real wall-clock sleep; a test injects a recording fake
        # so the delay is observed without sleeping.
        self._politeness_delay_seconds = politeness_delay_seconds
        self._sleep: SleepFn = sleep or _default_sleep
        # Optional factory for the real httpx client; defaults to a follow
        # redirects-off client with a 30s timeout. Tests inject a factory that
        # returns a client wired to an offline MockTransport so the actual
        # redirect-hop pacing branch is exercised without a socket.
        self._client_factory = client_factory

    def _seeds(self, config: ConnectorConfig) -> list[str]:
        raw = config.settings.get("urls")
        if not raw:
            raise ValueError("WebConnector requires settings.urls (list of seed URLs)")
        seeds = [str(u) for u in raw]
        for seed in seeds:
            if origin_of(seed) is None:
                raise ValueError(
                    f"seed URL must be plain http(s) with a host: {seed!r}"
                )
        return seeds

    def _allowed_origins(self, config: ConnectorConfig) -> set[str]:
        """Origins this connector may fetch (T-V1-034 origin policy).

        Seed origins, plus any operator-declared
        ``settings["allowed_origins"]`` (explicit cross-origin opt-in, e.g. a
        CDN host). An entry without a host/scheme is rejected rather than
        silently ignored.
        """
        origins = {origin_of(seed) for seed in self._seeds(config)}
        for extra in config.settings.get("allowed_origins") or []:
            origin = origin_of(str(extra))
            if origin is None:
                raise ValueError(f"allowed_origins entry is not http(s): {extra!r}")
            origins.add(origin)
        origins.discard(None)
        return {str(o) for o in origins}

    def _require_in_scope(self, config: ConnectorConfig, url: str) -> str:
        allowed = self._allowed_origins(config)
        origin = origin_of(url)
        if origin is None or origin not in allowed:
            raise OutOfScopeRedirect(f"URL outside the configured origins: {url!r}")
        return origin

    def _max_redirects(self, config: ConnectorConfig) -> int:
        raw = config.settings.get("max_redirects", DEFAULT_MAX_REDIRECTS)
        value = int(raw)
        if value < 0:
            raise ValueError("max_redirects must be >= 0")
        return value

    def _validate_politeness_delay(self, seconds: float) -> None:
        """Validate a politeness-delay value (seconds); raise if out of range.

        The delay is a bounded, non-negative quantity: negative values would
        stall the crawl backwards and a non-finite value would hang it. Same
        ``ValueError`` style as the other bounds so a bad config fails loudly
        at construction/configuration time, not mid-crawl.
        """
        if not math.isfinite(seconds):
            raise ValueError(f"politeness_delay must be finite, got {seconds!r}")
        if seconds < 0:
            raise ValueError(f"politeness_delay must be >= 0, got {seconds!r}")
        if seconds > _MAX_POLITENESS_DELAY_SECONDS:
            raise ValueError(
                f"politeness_delay must be <= {_MAX_POLITENESS_DELAY_SECONDS:g}s, got {seconds!r}"
            )

    def _resolve_politeness_delay(self, config: ConnectorConfig) -> float:
        """Seconds to wait between consecutive crawl fetches.

        Precedence: an explicit constructor value (already validated) wins over
        the per-connector ``settings``. Within settings, ``politeness_delay``
        (float seconds) is preferred; ``politeness_delay_ms`` (int ms) is a
        convenience alias for operators thinking in milliseconds. When neither
        is set the default (0 = no delay) applies, preserving the previous
        zero-delay behaviour so the bounded-crawl contract tests stay green.
        """
        if self._politeness_delay_seconds is not None:
            return self._politeness_delay_seconds
        if "politeness_delay" in config.settings:
            seconds = float(config.settings["politeness_delay"])
        elif "politeness_delay_ms" in config.settings:
            seconds = float(config.settings["politeness_delay_ms"]) / 1000.0
        else:
            return DEFAULT_POLITENESS_DELAY_SECONDS
        self._validate_politeness_delay(seconds)
        return seconds

    async def _fetch_bytes(
        self, config: ConnectorConfig, url: str
    ) -> tuple[bytes, dict[str, str]]:
        """Fetch ``url`` under the T-V1-034 origin + redirect policy.

        The scope check runs **before** any transport (so an injected seam
        cannot be used to reach an out-of-scope URL either), and the default
        httpx transport follows redirects manually — each hop re-validated —
        instead of letting the client silently change host.
        """
        allowed = self._allowed_origins(config)
        origin = origin_of(url)
        if origin is None or origin not in allowed:
            raise OutOfScopeRedirect(f"URL outside the configured origins: {url!r}")
        if self._fetch is not None:
            return await self._fetch(url)
        import httpx

        max_redirects = self._max_redirects(config)
        # Politeness delay is applied between redirect hops as well: a redirect
        # chain is still N separate HTTP requests to the host, so a one-page
        # /redirect -> /target crawl must pace its hops the same way it paces
        # inter-page fetches (once a hop has already been made, before the
        # next). Origin validation runs before any hop so a disallowed hop is
        # rejected before any transport is spent.
        politeness_delay = self._resolve_politeness_delay(config)
        if self._client_factory is not None:
            client = self._client_factory()
        else:
            client = httpx.AsyncClient(follow_redirects=False, timeout=30.0)
        try:
            current = url
            for hop in range(max_redirects + 1):
                if hop > 0 and politeness_delay > 0:
                    await self._sleep(politeness_delay)
                if self._client_factory is None:
                    _assert_public_host(current)
                resp = await client.get(current)
                location = resp.headers.get("location")
                if resp.status_code in _REDIRECT_STATUSES and location:
                    current = resolve_redirect(
                        current,
                        location,
                        allowed,
                        max_redirects=max_redirects,
                        hops=hop,
                    )
                    continue
                resp.raise_for_status()
                return resp.content, _default_headers(resp)
        finally:
            await client.aclose()
        raise ValueError(f"exceeded {max_redirects} redirects fetching {url}")

    def _discover_links(
        self, base: str, html: bytes, allowed_origins: set[str]
    ) -> list[str]:
        """Extract in-scope links from an HTML page (best-effort, stdlib re).

        Scope is an exact origin comparison (``origin_of``), never a string
        prefix: ``https://seed.test.evil.example/`` does not share an origin
        with ``https://seed.test/`` even though it starts with the same text.
        """
        import re

        links: list[str] = []
        for match in re.finditer(rb'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
            try:
                href = match.group(1).decode("utf-8", "ignore")
            except Exception:
                continue
            absolute = urljoin(base, href)
            if origin_of(absolute) in allowed_origins and absolute not in links:
                links.append(absolute)
        return links

    async def _crawl(self, config: ConnectorConfig) -> list[SourceItem]:
        """Breadth-first crawl from the seeds, bounded by depth/page caps.

        Returns items in discovery order (stable given the same inputs); the
        cursor then pages this deterministic list. The politeness delay is
        applied *before* each fetch attempt once at least one attempt has been
        made — counting **attempts**, not just successes — so a leading
        failed/dead request (404/timeout/DNS) still paces the gap and a dead
        link cannot be used to skip an opening burst. No trailing sleep is
        charged after the final attempt.
        """
        seeds = self._seeds(config)
        allowed_origins = self._allowed_origins(config)
        politeness_delay = self._resolve_politeness_delay(config)
        seen: set[str] = set()
        items: list[SourceItem] = []
        fetched_count = 0
        frontier: list[tuple[str, int]] = [(u, 0) for u in seeds]
        while frontier and len(items) < self._max_pages:
            url, depth = frontier.pop(0)
            if url in seen:
                continue
            seen.add(url)
            # Politeness delay is applied *before* the fetch attempt, once at
            # least one attempt has been made — pacing the gap between
            # consecutive requests regardless of outcome (0 = none, preserving
            # the prior zero-delay behaviour). A leading failed/dead request is
            # still an HTTP request to the host, so it must not skip a gap.
            if fetched_count > 0 and politeness_delay > 0:
                await self._sleep(politeness_delay)
            fetched_count += 1
            try:
                content, headers = await self._fetch_bytes(config, url)
            except OutOfScopeRedirect as exc:
                # Not a dead link: the source pointed (or redirected) outside
                # the configured origins. Loud, not silent — the run records
                # it so an operator can extend settings["allowed_origins"].
                logger.warning("web_crawl_out_of_scope", extra={"url": url, "reason": str(exc)})
                continue
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
                for link in self._discover_links(url, content, allowed_origins):
                    if link not in seen:
                        frontier.append((link, depth + 1))
        return items

    async def discover(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorDiscovery:
        all_items = sorted(await self._crawl(config), key=lambda it: it.external_id)
        window, next_cursor = page_items(all_items, cursor, self._page_size)
        return ConnectorDiscovery(
            items=window,
            next_cursor=next_cursor,
            snapshot=snapshot_digest(all_items),
        )

    async def fetch(self, config: ConnectorConfig, item: SourceItem) -> bytes:
        content, _headers = await self._fetch_bytes(config, item.uri or item.external_id)
        return content

    async def sync(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorSyncResult:
        discovery = await self.discover(config, cursor)
        politeness_delay = self._resolve_politeness_delay(config)
        fetched = 0
        attempted = 0
        errors: list[str] = []
        for item in discovery.items:
            if item.metadata.get("error"):
                errors.append(f"{item.external_id}: {item.metadata['error']}")
                continue
            # Politeness delay is applied *before* the re-fetch attempt, once at
            # least one attempt has been made — matching the crawl's pacing and
            # counting attempts (not just successes) so a leading failed
            # re-fetch still paces the gap that follows it.
            if attempted > 0 and politeness_delay > 0:
                await self._sleep(politeness_delay)
            attempted += 1
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
