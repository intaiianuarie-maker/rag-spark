"""Local folder connector (TASK-M6-001) — stdlib, no vendor SDK.

Discovers regular files under a configured directory in a **stable** order
(sorted by relative path), pages them with a cursor that never re-serves an
item, and fetches raw bytes. Designed to satisfy the shared connector contract
(``connectors.contract_tests.check_connector_contract``).

Incremental-sync hooks: ``SourceItem.size_bytes``/``updated_at`` and a content
checksum in ``metadata["checksum"]`` let the ingestion dedup (M2-001d) decide
whether a discovered item is new or changed. Cursor paging guarantees a resumable
scan that never duplicates work.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from ..interfaces import (
    Connector,
    ConnectorConfig,
    ConnectorDiscovery,
    ConnectorKind,
    ConnectorSyncResult,
    SourceItem,
)


class LocalFolderConnector(Connector):
    """A connector over a local directory tree."""

    kind = ConnectorKind.LOCAL_UPLOAD

    def __init__(self, page_size: int = 100) -> None:
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        self._page_size = page_size

    def _root(self, config: ConnectorConfig) -> Path:
        raw = config.settings.get("path")
        if not raw:
            raise ValueError("LocalFolderConnector requires settings.path")
        return Path(raw)

    def _scan(self, config: ConnectorConfig) -> list[SourceItem]:
        """List files under the root in a stable (sorted) order."""
        root = self._root(config)
        if not root.exists():
            raise FileNotFoundError(f"path does not exist: {root}")
        items: list[SourceItem] = []
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            stat = path.stat()
            data = path.read_bytes()
            items.append(
                SourceItem(
                    external_id=rel,
                    name=path.name,
                    uri=path.resolve().as_uri(),
                    mime_type=None,
                    size_bytes=stat.st_size,
                    updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    metadata={
                        "checksum": hashlib.sha256(data).hexdigest(),
                        "path": rel,
                    },
                )
            )
        return items

    async def discover(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorDiscovery:
        all_items = self._scan(config)
        start = int(cursor) if cursor else 0
        window = all_items[start : start + self._page_size]
        next_start = start + self._page_size
        next_cursor = str(next_start) if next_start < len(all_items) else None
        return ConnectorDiscovery(items=window, next_cursor=next_cursor)

    async def fetch(self, config: ConnectorConfig, item: SourceItem) -> bytes:
        root = self._root(config)
        # Resolve within the root; refuse traversal (external_id is the rel path).
        target = (root / item.external_id).resolve()
        if not str(target).startswith(str(root.resolve())):
            raise ValueError(f"path escapes root: {item.external_id}")
        if not target.is_file():
            raise KeyError(f"unknown external_id: {item.external_id}")
        return target.read_bytes()

    async def sync(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorSyncResult:
        discovery = await self.discover(config, cursor)
        fetched = 0
        for item in discovery.items:
            await self.fetch(config, item)
            fetched += 1
        return ConnectorSyncResult(
            discovered=len(discovery.items),
            fetched=fetched,
            next_cursor=discovery.next_cursor,
        )
