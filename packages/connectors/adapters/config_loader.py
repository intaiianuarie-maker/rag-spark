"""Load admitted persisted sources without importing the API application.

Worker images contain packages and apps/worker, so the runtime cannot depend
on app.sources or its ORM metadata. Both callers use this SQL adapter and the
same shared settings admission policy.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from core.config import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from ..config_validation import validate_settings
from ..interfaces import ConnectorConfig, ConnectorKind


def load_persisted_sources(
    workspace_id: str, *, session_factory: Any = None, database_url: str | None = None
) -> Sequence[ConnectorConfig]:
    workspace = uuid.UUID(workspace_id)
    engine = None
    if session_factory is None:
        url = database_url or get_settings().database_url
        engine = create_engine(url.replace("+asyncpg", "+psycopg"), pool_pre_ping=True)
        session_factory = lambda: Session(engine)  # noqa: E731
    try:
        with session_factory() as session:
            rows = session.execute(
                text("SELECT connector_id, kind, settings, credential_ref FROM connector_configs "
                     "WHERE workspace_id = :workspace_id ORDER BY connector_id"),
                {"workspace_id": workspace},
            ).mappings().all()
        configs = []
        for row in rows:
            if row["credential_ref"] is not None:
                raise ValueError("credential_ref is not supported by this adapter")
            settings = validate_settings(row["kind"], row["settings"])
            configs.append(ConnectorConfig(
                connector_id=row["connector_id"],
                workspace_id=str(workspace),
                kind=ConnectorKind(row["kind"]),
                settings=settings,
            ))
        return configs
    finally:
        if engine is not None:
            engine.dispose()


def make_db_source_loader(database_url: str) -> Callable[[str], Sequence[ConnectorConfig]]:
    def load(workspace_id: str) -> Sequence[ConnectorConfig]:
        return load_persisted_sources(workspace_id, database_url=database_url)

    return load
