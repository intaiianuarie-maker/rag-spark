"""S3 bucket connector (TASK-M6-001) — boto3 behind the connector seam.

Lists objects in an S3-compatible bucket in a **stable** order (by key), pages
them with a cursor that never re-serves an item, and downloads raw bytes.
Endpoint/credentials follow the PD-005 convention (``S3_ENDPOINT_URL`` /
``S3_ACCESS_KEY`` / ``S3_SECRET_KEY``) so SeaweedFS or any S3 backend works with
a one-command swap.

Designed to satisfy ``connectors.contract_tests.check_connector_contract``.
The boto3 client is injectable so the contract runs offline against a fake;
production passes a real endpoint. Incremental-sync hooks: object ETag/Size in
``SourceItem`` and ``updated_at`` from ``LastModified``.
"""

from __future__ import annotations

import os
from typing import Any

from botocore.config import Config

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


class S3Connector(Connector):
    """A connector over an S3-compatible bucket."""

    kind = ConnectorKind.S3

    def __init__(
        self,
        page_size: int = 100,
        injectable_client: Any | None = None,
    ) -> None:
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        self._page_size = page_size
        self._client = injectable_client

    def _bucket(self, config: ConnectorConfig) -> str:
        bucket = config.settings.get("bucket") or os.getenv("S3_BUCKET")
        if not bucket:
            raise ValueError("S3Connector requires settings.bucket or S3_BUCKET")
        return bucket

    def _prefix(self, config: ConnectorConfig) -> str:
        prefix = config.settings.get("prefix", "") or ""
        if any(ch in prefix for ch in "*?["):
            raise ValueError(
                "S3Connector prefix is a literal key prefix — glob/regex "
                "characters are not supported"
            )
        return prefix

    def _require_in_prefix(self, config: ConnectorConfig, key: str) -> None:
        """T-V1-034 S3 prefix semantics — a configured prefix is a hard scope.

        ``settings.prefix`` is a **literal, unnormalized key prefix**: a key is
        in scope iff ``key.startswith(prefix)``, with no glob, no regex, no
        implicit ``/`` and no delimiter (listing is recursive, so ``docs``
        also matches ``docs-old/x``; write ``docs/`` for a folder). Whatever
        the listing admits, ``fetch`` must refuse — an item that did not come
        from this prefix (or one whose key was rewritten by a caller) is out
        of scope and must not be downloaded.
        """
        prefix = self._prefix(config)
        if not key.startswith(prefix):
            raise ValueError(
                f"key {key!r} is outside the configured prefix {prefix!r}"
            )

    def _client_or_build(self):
        if self._client is not None:
            return self._client
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=os.getenv("S3_ENDPOINT_URL") or os.getenv("SEAWEEDFS_ENDPOINT"),
            aws_access_key_id=os.getenv("S3_ACCESS_KEY")
            or os.getenv("SEAWEEDFS_ACCESS_KEY"),
            aws_secret_access_key=os.getenv("S3_SECRET_KEY")
            or os.getenv("SEAWEEDFS_SECRET_KEY"),
            config=Config(signature_version="s3v4"),
        )

    def _list_all(self, config: ConnectorConfig) -> list[SourceItem]:
        """List all objects under the prefix in a stable (sorted by key) order.

        T-V1-034: the listing is **recursive** — no ``Delimiter`` is sent, so
        keys are returned at every depth under the literal prefix (see
        :meth:`_require_in_prefix` for the prefix contract).
        """
        client = self._client_or_build()
        bucket = self._bucket(config)
        prefix = self._prefix(config)

        items: list[SourceItem] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                items.append(
                    SourceItem(
                        external_id=key,
                        name=key.rsplit("/", 1)[-1] or key,
                        uri=f"s3://{bucket}/{key}",
                        mime_type=None,
                        size_bytes=obj.get("Size", 0),
                        updated_at=obj.get("LastModified"),
                        metadata={
                            "etag": (obj.get("ETag") or "").strip('"') or None,
                            "key": key,
                        },
                    )
                )
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        items.sort(key=lambda it: it.external_id)
        return items

    async def discover(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorDiscovery:
        all_items = self._list_all(config)
        window, next_cursor = page_items(all_items, cursor, self._page_size)
        return ConnectorDiscovery(
            items=window,
            next_cursor=next_cursor,
            snapshot=snapshot_digest(all_items),
        )

    async def fetch(self, config: ConnectorConfig, item: SourceItem) -> bytes:
        self._require_in_prefix(config, item.external_id)
        client = self._client_or_build()
        bucket = self._bucket(config)
        resp = client.get_object(Bucket=bucket, Key=item.external_id)
        body = resp["Body"]
        return body.read()

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
