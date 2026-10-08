"""SeaweedFS S3 adapter — boto3 behind the storage seam.

Boto3 (Apache-2.0 SDK) is permitted inside `packages/storage/adapters/`
per PD-005: the binding banned AGPL code (retired community repackage)
and SDK references *outside* adapters, not boto3 itself.
references *outside* adapters, not boto3 itself. This adapter uses
boto3 `client('s3')` with endpoint_url + SigV4 signing, delivering
real `put_object`/`get_object`/`delete_object`/`presign_url` (via
`generate_presigned_url`) and `list_objects` (via `list_objects_v2`).
Image unmodified — compose/env only; one-command backend swap = change
`S3_ENDPOINT_URL` and the adapter uses the new endpoint.
Reuse: SeaweedFS is Apache-2.0 (service, optional external).
"""

from __future__ import annotations

import os
from typing import Any

import boto3
from botocore.config import Config

from ..interfaces import StorageAdapter, StorageError, StorageObject


class SeaweedFSAdapter(StorageAdapter):
    """Boto3-backed S3 adapter behind the storage seam."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
        injectable_client: Any | None = None,
    ) -> None:
        # Env-driven endpoint enables the one-command backend swap.
        endpoint_raw = (
            endpoint_url or os.getenv("S3_ENDPOINT_URL")
            or os.getenv("SEAWEEDFS_ENDPOINT") or ""
        )
        self.endpoint_url = endpoint_raw.rstrip("/")
        self.access_key = (access_key or os.getenv("S3_ACCESS_KEY",
            os.getenv("SEAWEEDFS_ACCESS_KEY", "")) or "")
        self.secret_key = (secret_key or os.getenv("S3_SECRET_KEY",
            os.getenv("SEAWEEDFS_SECRET_KEY", "")) or "")
        self.region = region
        if injectable_client is not None:
            self.client = injectable_client
        else:
            self.client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url or None,
                aws_access_key_id=self.access_key or None,
                aws_secret_access_key=self.secret_key or None,
                region_name=region,
                config=Config(signature_version="s3v4"),
            )

    async def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> StorageObject:
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            extra_args = {}
            if metadata:
                extra_args["Metadata"] = {str(k): str(v) for k, v in metadata.items()}
            resp = await loop.run_in_executor(
                None,
                lambda: self.client.put_object(
                    Bucket=bucket, Key=key, Body=data, **extra_args
                ),
            )
            etag = resp.get("ETag", "").strip('"') or None
            return StorageObject(key=key, size_bytes=len(data), etag=etag)
        except Exception as exc:
            raise StorageError(f"put_object failed: {exc}") from exc

    async def get_object(self, bucket: str, key: str) -> bytes:
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: self.client.get_object(Bucket=bucket, Key=key),
            )
            return resp["Body"].read()
        except Exception as exc:
            raise StorageError(f"get_object failed: {exc}") from exc

    async def delete_object(self, bucket: str, key: str) -> bool:
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self.client.delete_object(Bucket=bucket, Key=key),
            )
            return True
        except self.client.exceptions.NoSuchKey:
            return False
        except Exception as exc:
            raise StorageError(f"delete_object failed: {exc}") from exc

    async def presign_url(
        self,
        bucket: str,
        key: str,
        operation: str = "get",
        expiry_seconds: int = 3600,
    ) -> str:
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: self.client.generate_presigned_url(
                    ClientMethod=f"{operation}_object",
                    Params={"Bucket": bucket, "Key": key},
                    ExpiresIn=expiry_seconds,
                    HttpMethod="GET" if operation == "get" else "PUT",
                ),
            )
            return resp
        except Exception as exc:
            raise StorageError(f"presign_url failed: {exc}") from exc

    async def aclose(self) -> None:
        if hasattr(self.client, "close"):
            self.client.close()

    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        max_keys: int = 1000,
    ) -> list[StorageObject]:
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: self.client.list_objects_v2(
                    Bucket=bucket,
                    Prefix=prefix,
                    MaxKeys=max_keys,
                ),
            )
            contents = resp.get("Contents", [])
            return [
                StorageObject(
                    key=content["Key"],
                    size_bytes=content.get("Size", 0),
                    etag=content.get("ETag", "").strip('"') or None,
                )
                for content in contents
            ]
        except Exception as exc:
            raise StorageError(f"list_objects failed: {exc}") from exc
