"""T-EVAL-HARNESS — production ingest leg (upload API → worker → collection).

R08 inserted documents directly into Postgres and Qdrant. That is a bypass: it
skips the parse → chunk → embed → upsert path the product actually runs, so a
green R08 says nothing about the production ingest path. This module drives the
real one:

    POST /v1/workspaces/{ws}/documents   (app.documents.upload)
        → IngestionJob (queued)
    ingest_worker.process_one(job_id)   (the real consumer)
        → parse, chunk, embed, upsert into the serving collection
    poll job status → READY

Everything is scratch-scoped: one freshly generated workspace, whose rows and
vectors are purged on exit. The class records a before/after point count so
**net-zero cleanup is proven**, not assumed.

Nothing here writes to a live/shared collection unless the caller explicitly
passes one; the default is R08's serving collection so the measurement matches
R08, but the workspace is always eval-only and always cleaned.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import pathlib
import sys
import time
import uuid
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in ("packages", "apps/api", "apps/worker", "."):
    _pstr = str((_REPO_ROOT / _p).resolve())
    if _pstr not in sys.path:
        sys.path.insert(0, _pstr)

from core.auth import Principal, Role  # noqa: E402

JOB_TERMINAL = {"ready", "failed"}


@dataclasses.dataclass
class ProductionIngestLeg:
    """Drive real uploads + real worker processing, then prove net-zero cleanup."""

    collection: str
    org_id: str
    job_poll_seconds: float = 0.5
    job_timeout_seconds: float = 300.0

    workspace_id: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    initial_point_count: int = 0
    _client: Any = None
    _app: Any = None
    _session_factory: Any = None
    _qdrant: Any = None
    _created_document_ids: list[str] = dataclasses.field(default_factory=list)

    async def __aenter__(self) -> ProductionIngestLeg:
        from app.documents.upload import router as upload_router
        from app.main import create_app
        from fastapi.testclient import TestClient
        from ingestion.persistence import get_session_factory
        from qdrant_client import AsyncQdrantClient

        self._qdrant = AsyncQdrantClient(url="http://localhost:6333")
        info = await self._qdrant.get_collection(self.collection)
        self.initial_point_count = int(info.points_count or 0)

        self._session_factory = get_session_factory()
        await self._ensure_workspace_row()

        self._app = create_app()
        self._app.include_router(upload_router)
        principal = Principal(
            user_id="v1-full-evaluator",
            email="eval@knowledge-os.local",
            is_authenticated=True,
            is_active=True,
            workspace_roles={self.workspace_id: Role.OWNER},
        )
        self._app.dependency_overrides[app_principal_dep()] = lambda: principal
        self._client = TestClient(self._app)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.cleanup()
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
        if self._qdrant is not None:
            with contextlib.suppress(Exception):
                await self._qdrant.close()

    # ------------------------------------------------------------------ #

    async def _ensure_workspace_row(self) -> None:
        """Create the scratch workspace row the upload route requires."""
        from sqlalchemy import text

        async with self._session_factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO workspaces (id, organization_id, name, created_at, updated_at)
                    VALUES (:ws, :org, :name, NOW(), NOW())
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                {"ws": self.workspace_id, "org": self.org_id, "name": "v1-full-eval"},
            )
            await session.commit()

    async def ingest(self, item: Any, *, job_timeout: float | None = None) -> Any:
        """Upload one item's context through the real route and run the worker.

        Returns an ``IngestedRef``-shaped object (see ``v1_full_harness``).
        """
        from v1_full_harness import IngestedRef, chunk_context, gold_chunks_for

        payload = item.context.encode("utf-8")
        res = self._client.post(
            f"/v1/workspaces/{self.workspace_id}/documents",
            files={"file": (f"{item.source_id}.txt", payload, "text/plain")},
            data={"title": item.source_id},
        )
        if res.status_code != 202:
            raise RuntimeError(f"upload refused: {res.status_code} {res.text}")
        job_id = res.json()["job_id"]
        document_id = res.json()["document_id"]

        await self._drive_worker(job_id, timeout=job_timeout)

        from v1_full_harness import evaluate_ref  # noqa: F401  (import parity check)

        chunk_ids = await self._chunk_ids_for(document_id)
        chunks = chunk_context(item.context)
        return IngestedRef(
            item=item,
            workspace_id=self.workspace_id,
            document_id=document_id,
            chunk_ids=chunk_ids,
            gold_chunk_ids=gold_chunks_for(item, chunk_ids, chunks),
            job_id=job_id,
        )

    async def _drive_worker(self, job_id: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """Run the REAL worker consumer until the job settles."""
        from jobs.ingest_worker import process_one

        limit = timeout if timeout is not None else self.job_timeout_seconds
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            with contextlib.suppress(Exception):
                await process_one(uuid.UUID(job_id))
            status = await self._job_status(job_id)
            if status in JOB_TERMINAL:
                if status == "failed":
                    raise RuntimeError(f"ingestion job {job_id} failed in the worker")
                return
            await asyncio.sleep(self.job_poll_seconds)
        raise TimeoutError(f"ingestion job {job_id} did not settle in time")

    async def _job_status(self, job_id: str) -> str:
        from sqlalchemy import text

        async with self._session_factory() as session:
            cur = await session.execute(
                text("SELECT status FROM ingestion_jobs WHERE id = :id"),
                {"id": job_id},
            )
            row = cur.fetchone()
        return str(row[0]) if row else "failed"

    async def _chunk_ids_for(self, document_id: str) -> list[str]:
        from sqlalchemy import text

        async with self._session_factory() as session:
            cur = await session.execute(
                text("SELECT id FROM chunks WHERE document_id = :did ORDER BY position"),
                {"did": document_id},
            )
            rows = cur.fetchall()
        return [str(r[0]) for r in rows]

    # ------------------------------------------------------------------ #

    async def cleanup(self) -> dict[str, Any]:
        """Purge every eval-created row and vector; return the net-zero proof."""
        from sqlalchemy import text

        purged = {"chunks": 0, "ingestion_jobs": 0, "documents": 0, "vectors": 0}
        if self._qdrant is not None:
            from qdrant_client import models as qmodels

            with contextlib.suppress(Exception):
                await self._qdrant.delete(
                    collection_name=self.collection,
                    points_selector=qmodels.FilterSelector(
                        filter=qmodels.Filter(
                            must=[
                                qmodels.FieldCondition(
                                    key="workspace_id",
                                    match=qmodels.MatchValue(value=self.workspace_id),
                                )
                            ]
                        )
                    ),
                )
                purged["vectors"] = 1

        async with self._session_factory() as session:
            for table in ("chunks", "ingestion_jobs", "documents"):
                try:
                    cur = await session.execute(
                        text(f"DELETE FROM {table} WHERE workspace_id = :ws"),
                        {"ws": self.workspace_id},
                    )
                    purged[table] = int(cur.rowcount or 0)
                except Exception:
                    purged[table] = 0
            with contextlib.suppress(Exception):
                await session.execute(
                    text("DELETE FROM workspaces WHERE id = :ws"), {"ws": self.workspace_id}
                )
            await session.commit()

        final = int((await self._qdrant.get_collection(self.collection)).points_count or 0)
        return {
            "purged": purged,
            "point_count_before": self.initial_point_count,
            "point_count_after": final,
            "net_zero": final == self.initial_point_count,
        }


def app_principal_dep() -> Any:
    """The principal dependency the upload route uses (imported lazily)."""
    from app.authz.principal import get_principal

    return get_principal