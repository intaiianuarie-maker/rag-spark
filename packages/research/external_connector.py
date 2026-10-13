"""Approved-connector fetch adapter, scoped snapshots, default OFF (T-V3-025).

SSRF-safe: deny private-net/file URLs, decoded-then-rechecked, redirect guard
re-validates EVERY hop. Default flag FALSE (opt-in per connector).

Provenance: every fetch stores URL + time + content-hash + licence + workspace
(a snapshot without provenance is unfetchable-for-audit).

Tests must NOT hit real network — inject a fake transport; refusal must come
from the validator, not from a timeout.
"""

from __future__ import annotations

import hashlib
import ipaddress
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

# ---------------------------------------------------------------------------
# Errors — typed, never silent
# ---------------------------------------------------------------------------


class ConnectorDisabledError(RuntimeError):
    """Connector is disabled (default OFF) — opt-in required."""


class SSRFBlockedError(RuntimeError):
    """URL blocked by SSRF validator (private net, file, encoded, redirect)."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"SSRF blocked {url!r}: {reason}")
        self.url = url
        self.reason = reason


class ExternalFetchError(RuntimeError):
    """Fetch failed (too many redirects, transport error)."""


# ---------------------------------------------------------------------------
# Snapshot — five provenance fields (all asserted)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalSnapshot:
    """One scoped fetch — five provenance fields for audit."""

    url: str
    fetched_at: str
    content_hash: str
    licence: str
    workspace_id: str
    content: str = ""
    status_code: int = 200


# ---------------------------------------------------------------------------
# SSRF validator — decoded-then-rechecked, every hop
# ---------------------------------------------------------------------------

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::1/128"),
]

_ALLOWED_SCHEMES = {"http", "https"}


def _extract_host(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    # urlparse handles brackets for IPv6, hostname strips them
    return host.lower().strip()


def _is_blocked_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Explicit blocked networks
    for net in _BLOCKED_NETWORKS:
        if ip in net:
            return True
    # ip.is_private covers 10/8,172.16/12,192.168/16, fc00::/7 etc, but also
    # covers CGNAT; we treat is_private as blocked for safety
    if ip.is_private:
        return True
    if ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    return False


def _is_blocked_host(host: str) -> bool:
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host in {"metadata", "metadata.google.internal"}:
        return True
    return _is_blocked_ip(host)


def validate_url_for_fetch(url: str) -> str:
    """Validate URL for SSRF — decoded-then-rechecked."""
    if not url or not url.strip():
        raise SSRFBlockedError(url, "empty url")
    decoded = urllib.parse.unquote(url)
    # Double-decode once more for %25 encoding tricks (%2527 -> %27 -> ')
    # One extra pass is enough for the %31%32%37 case; loop until stable for safety (max 2)
    for _ in range(2):
        nxt = urllib.parse.unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt

    parsed = urllib.parse.urlparse(decoded)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        raise SSRFBlockedError(url, "file:// denied")
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFBlockedError(url, f"scheme {scheme!r} denied; only http/https allowed")

    host = _extract_host(decoded)
    if not host:
        raise SSRFBlockedError(url, "missing host")
    if _is_blocked_host(host):
        raise SSRFBlockedError(url, f"private host {host!r} denied")

    # Also check raw host before decode for encoded loopback (already covered by decoded check,
    # but keep for audit trail — decoded host is what will be dialled)
    return decoded


# ---------------------------------------------------------------------------
# Transport seam — fake injected in tests, never real network
# ---------------------------------------------------------------------------


class FetchTransport(Protocol):
    """Injected transport — tests provide a fake, never a real socket."""

    async def fetch(self, url: str) -> FakeResponse:
        ...


@dataclass
class FakeResponse:
    """Minimal response for the fake transport."""

    url: str
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    content: str = ""

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers


# ---------------------------------------------------------------------------
# Connector — default OFF, scoped snapshots, redirect guard
# ---------------------------------------------------------------------------


class ExternalConnector:
    """Approved-connector fetch adapter — default OFF, opt-in per instance."""

    def __init__(
        self,
        workspace_id: str,
        licence: str,
        enabled: bool = False,
        transport: Callable[[str], Awaitable[FakeResponse]] | FetchTransport | None = None,
        max_redirects: int = 3,
    ) -> None:
        if not workspace_id or not workspace_id.strip():
            raise ValueError("workspace_id must be non-empty")
        if not licence or not licence.strip():
            raise ValueError("licence must be non-empty")
        self.workspace_id = workspace_id
        self.licence = licence
        self.enabled = enabled
        self._transport = transport
        self.max_redirects = max_redirects
        self._snapshots: list[ExternalSnapshot] = []

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    @property
    def snapshots(self) -> list[ExternalSnapshot]:
        return list(self._snapshots)

    async def fetch(self, url: str) -> ExternalSnapshot:
        if not self.enabled:
            raise ConnectorDisabledError("connector is disabled — opt-in required (default OFF)")
        # Decoded-then-rechecked — validator is the gate, not the network
        decoded = validate_url_for_fetch(url)
        if self._transport is None:
            raise ExternalFetchError("no transport injected — tests must provide a fake transport")

        current = decoded
        for hop in range(self.max_redirects + 1):
            # Re-validate EVERY hop (redirect to 169.254.169.254 is classic bypass)
            validate_url_for_fetch(current)
            # Dispatch via injected transport — never a real socket
            maybe = self._transport
            if hasattr(maybe, "fetch"):
                resp: FakeResponse = await maybe.fetch(current)
            else:
                resp = await maybe(current)

            if resp.is_redirect:
                loc = resp.headers.get("Location") or resp.headers.get("location") or ""
                if not loc:
                    raise ExternalFetchError(f"redirect without Location at hop {hop}: {current!r}")
                nxt = urllib.parse.urljoin(current, loc)
                decoded_nxt = urllib.parse.unquote(nxt)
                # Re-validate the redirect target before following
                validate_url_for_fetch(decoded_nxt)
                if hop == self.max_redirects:
                    raise ExternalFetchError("too many redirects")
                current = decoded_nxt
                continue

            # Success — build provenance snapshot (all five fields)
            body = resp.content if isinstance(resp.content, str) else str(resp.content)
            content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            snap = ExternalSnapshot(
                url=url,
                fetched_at=datetime.now(UTC).isoformat(),
                content_hash=content_hash,
                licence=self.licence,
                workspace_id=self.workspace_id,
                content=body,
                status_code=resp.status_code,
            )
            self._snapshots.append(snap)
            return snap

        raise ExternalFetchError("too many redirects")
