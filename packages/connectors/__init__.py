"""Connectors package.

Contracts live in :mod:`connectors.interfaces`. Adapters live under
``connectors.adapters/`` and must satisfy the shared contract in
:mod:`connectors.contract_tests` (stable discovery order, cursor paging that
never re-serves an item, honest sync counts, loud failure on missing items).

M6-001 adapters:
- ``LocalFolderConnector`` (stdlib) — a local directory tree.
- ``S3Connector`` (boto3) — an S3-compatible bucket (reuses the PD-005 storage
  seam's endpoint/credentials convention).

Web (Crawlee) and Git (LlamaHub/dulwich) connectors are declared in the
interface but are NOT wired here: their vendor deps are not part of the current
environment, so they would ship untested. They are additive when the deps land.
"""

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
    "SourceItem",
]
