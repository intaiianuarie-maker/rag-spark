"""Connector contracts (docs/reuse/V1_COMPONENTS.md).

v1 connector set: local upload, web URL/crawler, S3, Git repository. Future
implementations (NOT wired in M0) may wrap the LlamaIndex/Haystack connector
ecosystem or direct provider SDKs, but must normalize output into our ingestion
document model behind this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
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
        """List available source items, resuming from ``cursor`` if given."""
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
