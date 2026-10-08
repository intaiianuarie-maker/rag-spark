"""Connectors package.

Contracts live in :mod:`connectors.interfaces`. Adapters live under
``connectors.adapters/`` and must satisfy the shared contract in
:mod:`connectors.contract_tests` (stable discovery order, cursor paging that
never re-serves an item, honest sync counts, loud failure on missing items).

M6-001 adapters (all wired, all contract-tested):
- ``LocalFolderConnector`` (stdlib) — a local directory tree.
- ``S3Connector`` (boto3) — an S3-compatible bucket (reuses the PD-005 storage
  seam's endpoint/credentials convention).
- ``WebConnector`` (httpx) — crawl a bounded set of same-origin pages.
- ``GitConnector`` (git CLI subprocess) — read files tracked at a repo ref.

Only ``connectors.adapters/`` may touch vendor SDKs or shell out; no SDK/vendor
type crosses the ``connectors.interfaces`` seam. Use :func:`create_connector`
to resolve a ``Connector`` from a ``ConnectorConfig``.
"""

from .adapters import GitConnector, LocalFolderConnector, S3Connector, WebConnector
from .interfaces import (
    Connector,
    ConnectorConfig,
    ConnectorDiscovery,
    ConnectorKind,
    ConnectorSyncResult,
    SourceItem,
)

__all__ = [
    "Connector",
    "ConnectorConfig",
    "ConnectorDiscovery",
    "ConnectorKind",
    "ConnectorSyncResult",
    "GitConnector",
    "LocalFolderConnector",
    "S3Connector",
    "SourceItem",
    "WebConnector",
    "create_connector",
]


def create_connector(config: ConnectorConfig) -> Connector:
    """Resolve a ``Connector`` for ``config.kind`` (single wiring point)."""
    if config.kind is ConnectorKind.LOCAL_UPLOAD:
        return LocalFolderConnector()
    if config.kind is ConnectorKind.S3:
        return S3Connector()
    if config.kind is ConnectorKind.WEB:
        return WebConnector()
    if config.kind is ConnectorKind.GIT:
        return GitConnector()
    raise ValueError(f"unsupported connector kind: {config.kind}")
