"""Database engine + Alembic migration runner.

SQLAlchemy 2.x and Alembic are core framework dependencies (per
docs/engineering/coding-standards.md: "SQLAlchemy/Alembic for persistence").
The concrete psycopg driver is selected only through the DB URL, so no vendor
driver is imported directly here.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from core.config import get_settings
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

APP_DIR = Path(__file__).resolve().parent
API_DIR = APP_DIR.parent
ALEMBIC_INI = API_DIR / "alembic.ini"
ALEMBIC_DIR = API_DIR / "alembic"

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """Return the lazily-created process-wide async engine.

    Engine creation does not open a connection, so this is safe even when the
    database is down (readiness reports the failure instead of crashing).
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            connect_args={"connect_timeout": 5},
        )
    return _engine


async def dispose_engine() -> None:
    """Dispose the shared engine on shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def build_alembic_config() -> Config:
    """Build an Alembic config pointing at the repo's migrations."""
    settings = get_settings()
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def run_migrations() -> None:
    """Apply ``alembic upgrade head`` (called on API boot, non-fatal)."""
    command.upgrade(build_alembic_config(), "head")
