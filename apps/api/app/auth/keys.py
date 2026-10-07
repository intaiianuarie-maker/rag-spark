"""Workspace-scoped API keys (M1-001c).

Storage rule (ticket "Out"): a key is stored as **prefix + argon2 hash only**;
the full secret is returned exactly once, on creation, and is never re-displayed
or logged. Auth events log the key id/prefix, never the secret or hash.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from core.auth import Role
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, String, Text, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.models import Base
from app.auth.passwords import hash_password, verify_password

logger = logging.getLogger("knowledge_os.api.auth.keys")

API_KEY_PREFIX = "kio_"
_PREFIX_BYTES = 6  # -> 12 hex chars
NAME_MAX = 120


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---- schemas ----------------------------------------------------------------


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    role: Role = Role.VIEWER
    expires_at: datetime | None = None


class ApiKeyPublic(BaseModel):
    id: str
    name: str
    prefix: str
    role: Role
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreated(ApiKeyPublic):
    #: Full secret, present only in the creation response.
    key: str


# ---- key material -----------------------------------------------------------


@dataclass(frozen=True)
class KeyMaterial:
    full_key: str
    prefix: str
    key_hash: str


def generate_key_material() -> KeyMaterial:
    prefix = secrets.token_hex(_PREFIX_BYTES)
    secret = secrets.token_urlsafe(32)
    full_key = f"{API_KEY_PREFIX}{prefix}_{secret}"
    return KeyMaterial(full_key=full_key, prefix=prefix, key_hash=hash_password(full_key))


def looks_like_api_key(token: str) -> bool:
    return token.startswith(API_KEY_PREFIX)


def extract_prefix(presented: str) -> str | None:
    if not presented.startswith(API_KEY_PREFIX):
        return None
    prefix, sep, secret = presented[len(API_KEY_PREFIX) :].partition("_")
    if not sep or not prefix or not secret:
        return None
    return prefix


async def get_active_key(session: AsyncSession, presented: str) -> ApiKey | None:
    """Return the matching, non-revoked, non-expired key, or ``None``."""
    prefix = extract_prefix(presented)
    if prefix is None:
        return None
    statement = select(ApiKey).where(ApiKey.prefix == prefix)
    key = (await session.execute(statement)).scalar_one_or_none()
    if key is None or key.revoked_at is not None:
        return None
    if key.expires_at is not None and key.expires_at <= datetime.now(UTC):
        return None
    if not verify_password(key.key_hash, presented):
        return None
    return key


# ---- service ----------------------------------------------------------------


class KeyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        created_by: uuid.UUID,
        name: str,
        role: Role,
        expires_at: datetime | None,
    ) -> tuple[ApiKey, str]:
        material = generate_key_material()
        key = ApiKey(
            workspace_id=workspace_id,
            created_by=created_by,
            name=name,
            prefix=material.prefix,
            key_hash=material.key_hash,
            role=role.value,
            expires_at=expires_at,
        )
        self._session.add(key)
        await self._session.commit()
        await self._session.refresh(key)
        logger.info(
            "api_key_created",
            extra={
                "event": "api_key_created",
                "key_id": str(key.id),
                "prefix": key.prefix,
                "workspace_id": str(workspace_id),
                "role": key.role,
            },
        )
        return key, material.full_key

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[ApiKey]:
        statement = (
            select(ApiKey)
            .where(ApiKey.workspace_id == workspace_id)
            .order_by(ApiKey.created_at, ApiKey.id)
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def revoke(self, workspace_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey | None:
        statement = select(ApiKey).where(
            ApiKey.id == key_id, ApiKey.workspace_id == workspace_id
        )
        key = (await self._session.execute(statement)).scalar_one_or_none()
        if key is None:
            return None
        if key.revoked_at is None:
            key.revoked_at = datetime.now(UTC)
            await self._session.commit()
            await self._session.refresh(key)
            logger.info(
                "api_key_revoked",
                extra={
                    "event": "api_key_revoked",
                    "key_id": str(key.id),
                    "prefix": key.prefix,
                    "workspace_id": str(workspace_id),
                },
            )
        return key
