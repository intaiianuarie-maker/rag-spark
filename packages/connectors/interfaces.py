"""Connector contracts (docs/reuse/V1_COMPONENTS.md).

v1 connector set: local upload, web URL/crawler, S3, Git repository. Future
implementations (NOT wired in M0) may wrap the LlamaIndex/Haystack connector
ecosystem or direct provider SDKs, but must normalize output into our ingestion
document model behind this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ConnectorKind(StrEnum):
    LOCAL_UPLOAD = "local_upload"
    WEB = "web"
    S3 = "s3"
    GIT = "git"


class ConnectorConfig(BaseModel):
    connector_id: str
    workspace_id: str
    kind: ConnectorKind
    settings: dict[str, Any] = Field(default_factory=dict)


class SourceItem(BaseModel):
    """One external item discovered by a connector (pre-download)."""

    external_id: str
    name: str
    uri: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorDiscovery(BaseModel):
    items: list[SourceItem] = Field(default_factory=list)
    next_cursor: str | None = None
    #: T-V1-034 snapshot identity: a digest of the FULL listing the page was
    #: cut from (every ``external_id`` + its best change signal), so a resumed
    #: run can tell "the source changed mid-run" from "same snapshot, more
    #: pages left". ``None`` means the adapter could not describe its listing.
    snapshot: str | None = None


#: Keyset cursor prefix (T-V1-034). ``k:<external_id>`` means "serve the next
#: items strictly after this key in ``external_id`` order". A numeric cursor is
#: the LEGACY *offset* form and exists only for backward compatibility: offsets
#: shift when items are inserted or deleted between runs, which is exactly the
#: rescan bug this ticket closes. Never persist an offset as sync progress.
KEYSET_PREFIX = "k:"


def keyset_cursor(last_external_id: str) -> str:
    """Build the keyset cursor that resumes *after* ``last_external_id``."""
    if not last_external_id:
        raise ValueError("keyset cursor needs a non-empty external_id")
    return KEYSET_PREFIX + last_external_id


def is_keyset_cursor(cursor: str | None) -> bool:
    """True for the persisted ``k:<external_id>`` resume form."""
    return cursor is not None and cursor.startswith(KEYSET_PREFIX)


def page_items(
    items: Sequence[SourceItem], cursor: str | None, page_size: int
) -> tuple[list[SourceItem], str | None]:
    """Window an ordered listing for ``cursor`` (T-V1-034 keyset paging).

    ``items`` MUST be sorted by ``external_id`` — that total order is what makes
    a keyset resume independent of insert/delete churn before the cursor.
    Accepted cursors:

    - ``None`` → first window, offset-form ``next_cursor`` (legacy shape);
    - ``k:<id>`` → items strictly after ``<id>``, keyset-form ``next_cursor``;
    - a plain integer string → legacy offset (pre-T-V1-034 callers only).

    Anything else raises loudly rather than silently restarting the scan.
    """
    if page_size < 1:
        raise ValueError(f"page_size must be >= 1, got {page_size}")
    keyset = is_keyset_cursor(cursor)
    if cursor is None:
        start = 0
    elif keyset:
        key = cursor[len(KEYSET_PREFIX) :]
        if not key:
            raise ValueError("empty keyset cursor")
        start = 0
        while start < len(items) and items[start].external_id <= key:
            start += 1
    else:
        try:
            start = int(cursor)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported cursor: {cursor!r}") from exc
        if start < 0:
            raise ValueError(f"cursor must be >= 0, got {start}")
    end = min(start + page_size, len(items))
    window = list(items[start:end])
    if end >= len(items):
        return window, None
    if keyset:
        return window, keyset_cursor(window[-1].external_id)
    return window, str(end)


def snapshot_digest(items: Sequence[SourceItem]) -> str:
    """Digest the FULL listing: identity + best change signal per item.

    Change signal preference is adapter-owned metadata (content checksum, git
    blob SHA, S3 ETag) falling back to size, so a snapshot changes when the
    source's contents change — not merely when the page window does.
    """
    import hashlib

    rows = []
    for item in items:
        meta = item.metadata
        signal = (
            meta.get("checksum")
            or meta.get("sha")
            or meta.get("etag")
            or item.size_bytes
            or ""
        )
        rows.append(f"{item.external_id}\t{signal}")
    payload = "\n".join(rows).encode("utf-8", "surrogateescape")
    return hashlib.sha256(payload).hexdigest()


class ConnectorSyncResult(BaseModel):
    discovered: int = 0
    fetched: int = 0
    next_cursor: str | None = None
    errors: list[str] = Field(default_factory=list)


class Connector(ABC):
    """Adapter boundary for external sources.

    Output is normalized into the ingestion pipeline; no vendor types leak.
    """

    kind: ConnectorKind

    @abstractmethod
    async def discover(
        self,
        config: ConnectorConfig,
        cursor: str | None = None,
    ) -> ConnectorDiscovery:
        """List available source items, resuming from ``cursor`` if given.

        T-V1-034 cursor contract (see :data:`KEYSET_PREFIX` / :func:`page_items`):
        listings are ordered by ``external_id`` and the consumer persists a
        **keyset** cursor (``k:<external_id>``), never a positional offset —
        an offset shifts when the source changes between runs and skips items.
        Adapters still return the legacy offset form when started with no
        cursor so pre-existing callers keep their exact shape; anything a
        caller intends to resume from must be round-tripped through
        :func:`keyset_cursor`. ``snapshot`` describes the full listing the
        page came from.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch(self, config: ConnectorConfig, item: SourceItem) -> bytes:
        """Download one item's bytes for parsing."""
        raise NotImplementedError

    @abstractmethod
    async def sync(
        self,
        config: ConnectorConfig,
        cursor: str | None = None,
    ) -> ConnectorSyncResult:
        """Discover + fetch, tracking sync state for incremental runs."""
        raise NotImplementedError
