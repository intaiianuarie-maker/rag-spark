"""Storage adapter interface — backend-agnostic S3 storage seam.

Only the adapter touches vendor SDKs; no SDK types cross this boundary.
Used by the connector pipeline (TASK-M6-001) and ingestion persistence.
Binding per PD-005 (decided 2026-10-08): adapter-only S3 access,
SeaweedFS optional external service, image unmodified (compose/env only),
one-command backend swap documented, vendor guard + grep pin verifies
no AGPL module vendored and no SDK outside adapters/.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class StorageError(Exception):
    """Typed storage error — never leaks vendor exceptions."""


class StorageObject(BaseModel):
    key: str
    size_bytes: int = 0
    etag: str | None = None


class StorageAdapter(ABC):
    """S3-compatible storage adapter behind the storage seam."""

    @abstractmethod
    async def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> StorageObject:
        ...

    @abstractmethod
    async def get_object(self, bucket: str, key: str) -> bytes:
        ...

    @abstractmethod
    async def delete_object(self, bucket: str, key: str) -> bool:
        ...

    @abstractmethod
    async def presign_url(
        self,
        bucket: str,
        key: str,
        operation: str = "get",
        expiry_seconds: int = 3600,
    ) -> str:
        ...

    @abstractmethod
    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        max_keys: int = 1000,
    ) -> list[StorageObject]:
        ...
