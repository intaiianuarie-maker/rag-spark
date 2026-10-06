"""Async database session wiring for the auth module.

Reuses the process-wide engine from ``app.db`` (SQLAlchemy 2.x). This is core
persistence framework code, not a vendor SDK, so it is not an adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_engine

_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped async session."""
    factory = get_session_factory()
    async with factory() as session:
        yield session
