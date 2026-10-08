"""Postgres FTS lexical store — tsvector/GIN, RO/PT/EN configs.

Reuse: PostgreSQL built-in FTS (PostgreSQL license, see REUSE_MAP.md).
Adapter speaks Postgres via SQLAlchemy async + psycopg; no PG type crosses the
LexicalStore ABC boundary — we return plain RetrievedItem.

Design: table `lexical_docs` (migration 0007) with tsvector + GIN, tsv_cfg
column for cross-language recall, page/section for citation provenance.
SearchFilter → WHERE clause applied *before* ranking (R-004).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text

from ..interfaces import LexicalStore, RetrievalMethod, RetrievedItem, SearchFilter, VectorRecord

_LANG_CONFIG = {
    "ro": "romanian",
    "pt": "portuguese",
    "en": "english",
    "english": "english",
    "romanian": "romanian",
    "portuguese": "portuguese",
}

_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _config_for(lang: str | None) -> str:
    if not lang:
        return "simple"
    return _LANG_CONFIG.get(lang.lower(), "simple")


def _to_uuid(s: str) -> str:
    try:
        uuid.UUID(s)
        return s
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, s))


def _validate_metadata_keys(metadata: dict[str, Any]) -> None:
    for k in metadata:
        if not _KEY_RE.match(k):
            raise ValueError(f"metadata key not allowed: {k!r} (allow ^[A-Za-z0-9_]+$)")


class PostgresLexicalStore(LexicalStore):
    """Postgres FTS store — implements retrieval.interfaces.LexicalStore."""

    def __init__(self, session_factory=None) -> None:
        if session_factory is None:
            from ingestion.persistence import get_session_factory

            self._session_factory = get_session_factory()
        else:
            self._session_factory = session_factory

    async def index(self, *, records: Sequence[VectorRecord]) -> int:
        if not records:
            return 0
        async with self._session_factory() as session:
            for r in records:
                _validate_metadata_keys(r.metadata or {})
                cfg = _config_for(r.metadata.get("language") or r.metadata.get("lang"))
                pid = _to_uuid(str(r.id))
                await session.execute(
                    text("DELETE FROM lexical_docs WHERE id = :id"),
                    {"id": pid},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO lexical_docs
                          (id, workspace_id, document_id, document_version_id,
                           chunk_id, content, language, metadata, tsv, tsv_cfg,
                           page, section)
                        VALUES
                          (:id, :ws, :did, :dvid, :cid, :content, :lang,
                           CAST(:meta AS jsonb),
                           to_tsvector(CAST(:cfg AS regconfig), :content),
                           :cfg, :page, :section)
                        """
                    ),
                    {
                        "id": pid,
                        "ws": r.workspace_id,
                        "did": r.document_id,
                        "dvid": r.document_version_id,
                        "cid": r.chunk_id,
                        "content": r.content,
                        "lang": r.metadata.get("language") or r.metadata.get("lang"),
                        "meta": json.dumps(r.metadata or {}),
                        "cfg": cfg,
                        "page": r.page,
                        "section": r.section,
                    },
                )
            await session.commit()
        return len(records)

    async def search(
        self,
        *,
        query: str,
        top_k: int,
        query_filter: SearchFilter,
    ) -> list[RetrievedItem]:
        if not query_filter.workspace_ids:
            return []
        if not query.strip():
            return []
        async with self._session_factory() as session:
            where = ["workspace_id = ANY(:wss)"]
            params: dict[str, Any] = {
                "wss": query_filter.workspace_ids,
                "q": query,
                "k": top_k,
            }
            if query_filter.document_ids:
                where.append("document_id = ANY(:dids)")
                params["dids"] = query_filter.document_ids
            if query_filter.language:
                where.append("language = :lang")
                params["lang"] = query_filter.language
            for k, v in (query_filter.metadata or {}).items():
                if not _KEY_RE.match(k):
                    raise ValueError(f"metadata key not allowed: {k!r}")
                where.append(f"metadata->>'{k}' = :meta_{k}")
                params[f"meta_{k}"] = str(v)

            # Cross-language recall: when no language filter, use per-row tsv_cfg
            # so a RO-indexed row is queried with romanian, not english default.
            if query_filter.language:
                cfg = _config_for(query_filter.language)
                where.append("tsv @@ plainto_tsquery(CAST(:cfg AS regconfig), :q)")
                params["cfg"] = cfg
            else:
                where.append("tsv @@ plainto_tsquery(tsv_cfg::regconfig, :q)")

            where_sql = " AND ".join(where)
            sql = text(
                f"""
                SELECT id, workspace_id, document_id, document_version_id,
                       chunk_id, content, language, metadata, page, section,
                       ts_rank(tsv, plainto_tsquery(
                         COALESCE(tsv_cfg, 'simple')::regconfig, :q)) AS score
                FROM lexical_docs
                WHERE {where_sql}
                ORDER BY score DESC
                LIMIT :k
                """
            )
            result = await session.execute(sql, params)
            rows = result.mappings().all()
            items: list[RetrievedItem] = []
            for row in rows:
                items.append(
                    RetrievedItem(
                        content=str(row["content"]),
                        score=float(row["score"] or 0.0),
                        method=RetrievalMethod.LEXICAL,
                        workspace_id=row["workspace_id"],
                        document_id=row["document_id"],
                        document_version_id=row["document_version_id"],
                        chunk_id=row["chunk_id"],
                        page=row["page"],
                        section=row["section"],
                        scores={"lexical": float(row["score"] or 0.0)},
                        metadata=dict(row["metadata"] or {}),
                    )
                )
            return items

    async def delete(
        self,
        *,
        ids: Sequence[str] | None = None,
        query_filter: SearchFilter | None = None,
    ) -> int:
        async with self._session_factory() as session:
            if ids:
                qids = [_to_uuid(str(i)) for i in ids]
                result = await session.execute(
                    text("DELETE FROM lexical_docs WHERE id = ANY(:ids)"),
                    {"ids": qids},
                )
                await session.commit()
                return int(result.rowcount or 0)  # type: ignore[attr-defined]
            if query_filter is not None:
                if not query_filter.workspace_ids:
                    return 0
                result = await session.execute(
                    text("DELETE FROM lexical_docs WHERE workspace_id = ANY(:wss)"),
                    {"wss": query_filter.workspace_ids},
                )
                await session.commit()
                return int(result.rowcount or 0)  # type: ignore[attr-defined]
            return 0


def create_lexical_store(**kwargs: Any) -> LexicalStore:
    """Harness factory — frozen M0-04 contract (tests/README.md)."""
    return PostgresLexicalStore(**kwargs)
