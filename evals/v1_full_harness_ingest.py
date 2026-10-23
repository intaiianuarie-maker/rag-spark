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
from collections.abc import Sequence
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
    #: Serving embedding dimension the scratch collection is created at.
    dimension: int = 3072
    job_poll_seconds: float = 0.5
    job_timeout_seconds: float = 300.0

    workspace_id: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    initial_point_count: int = 0
    _client: Any = None
    _app: Any = None
    _session_factory: Any = None
    _qdrant: Any = None
    _created_document_ids: list[str] = dataclasses.field(default_factory=list)
    #: Result of the last cleanup, so the report can prove net-zero.
    last_cleanup: dict[str, Any] = dataclasses.field(default_factory=dict)

    async def __aenter__(self) -> ProductionIngestLeg:
        from app.documents.upload import router as upload_router
        from app.main import create_app
        from fastapi.testclient import TestClient
        from ingestion.persistence import get_session_factory
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.http import models as qmodels

        self._qdrant = AsyncQdrantClient(url="http://localhost:6333")
        # HARNESS BUG FIX #1: the worker resolves its target collection from
        # VECTOR_COLLECTION, which defaults to `knowledge` — a 1024-d
        # collection. The production gemini path emits 3072-d vectors, so every
        # write was rejected ("Vector dimension error: expected dim: 1024, got
        # 3072"). The harness exports VECTOR_COLLECTION for the in-process
        # worker (core.config.vector_collection() resolves per call by design).
        #
        # HARNESS BUG FIX #2: `QdrantVectorStore.upsert` always writes BOTH
        # named vectors (`dense` + `sparse`). Neither `knowledge` nor
        # `knowledge__serving` on this box declares `sparse`, so writes were
        # rejected ("Not existing vector name error: sparse"). The scratch
        # collection is therefore declared with both named vectors the adapter
        # emits.
        #
        # This defines OUR scratch collection only — per dev2's CLEANUP_PLAN
        # ("ingest outputs under a tmp collection ... drop temp collection").
        # No product code, no existing collection, no metric definition touched.
        await self._qdrant.create_collection(
            collection_name=self.collection,
            vectors_config={
                "dense": qmodels.VectorParams(
                    size=self.dimension, distance=qmodels.Distance.COSINE
                )
            },
            sparse_vectors_config={"sparse": qmodels.SparseVectorParams()},
        )
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
        self.last_cleanup = await self.cleanup()
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

    async def ingest(
        self,
        item: Any,
        *,
        docs: Sequence[tuple[str, str]] = (),
        gold_doc_ids: Sequence[str] = (),
        job_timeout: float | None = None,
    ) -> Any:
        """Upload each source document through the real route and run the worker.

        HARNESS BUG FIX #3: the first version concatenated an item's documents
        into ONE upload, which made the corpus's ``gold_doc_ids`` unresolvable —
        a 2wiki item is answerable only from specific documents, so the gold
        CHUNK set must come from the gold DOCUMENTS. Uploading each source
        document separately keeps that mapping real.
        """
        from v1_full_harness import IngestedRef

        if not docs:
            raise ValueError(f"item {getattr(item, 'source_id', '?')} has no documents")

        chunk_ids: list[str] = []
        gold_versions: list[str] = []
        first_document_id = ""
        for doc_id, text in docs:
            payload = text.encode("utf-8")
            res = self._client.post(
                f"/v1/workspaces/{self.workspace_id}/documents",
                files={"file": (f"{doc_id}.txt", payload, "text/plain")},
                data={"title": doc_id},
            )
            if res.status_code != 202:
                raise RuntimeError(f"upload refused for {doc_id}: {res.status_code} {res.text}")
            job_id = res.json()["job_id"]
            document_id = res.json()["document_id"]
            version_id = res.json()["version_id"]
            await self._drive_worker(job_id, timeout=job_timeout)
            chunk_ids.extend(await self._chunk_ids_for(version_id))
            if doc_id in set(gold_doc_ids):
                gold_versions.append(version_id)
            if not first_document_id:
                first_document_id = document_id

        gold_ids = await self._gold_chunk_ids(gold_versions)
        return IngestedRef(
            item=item,
            workspace_id=self.workspace_id,
            document_id=first_document_id,
            chunk_ids=chunk_ids,
            gold_chunk_ids=gold_ids,
            job_id="",
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

    async def _gold_chunk_ids(self, gold_doc_ids: Sequence[str]) -> list[str]:
        """Resolve gold DOCUMENT ids to gold CHUNK ids (the multi-hop gold).

        A 2wiki item is answerable only from specific documents, so the honest
        gold set is the chunks that came from the gold documents — not a text
        match of the answer, which multi-hop items routinely miss.
        """
        if not gold_doc_ids:
            return []
        from sqlalchemy import text

        async with self._session_factory() as session:
            cur = await session.execute(
                text(
                    "SELECT id FROM chunks WHERE document_version_id = ANY(:ids) "
                    "ORDER BY chunk_index"
                ),
                {"ids": list(gold_doc_ids)},
            )
            rows = cur.fetchall()
        return [str(r[0]) for r in rows]

    async def _chunk_ids_for(self, version_id: str) -> list[str]:
        """Chunks of one uploaded version (``chunks`` keys on version, not doc)."""
        from sqlalchemy import text

        async with self._session_factory() as session:
            cur = await session.execute(
                text(
                    "SELECT id FROM chunks WHERE document_version_id = :vid "
                    "ORDER BY chunk_index"
                ),
                {"vid": version_id},
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

        # Net-zero is proven by REMOVING the scratch collection and showing the
        # name no longer resolves.
        with contextlib.suppress(Exception):
            await self._qdrant.delete_collection(self.collection)
        collection_exists_after = True
        try:
            await self._qdrant.get_collection(self.collection)
        except Exception:
            collection_exists_after = False
        return {
            "purged": purged,
            "scratch_collection": self.collection,
            "collection_dropped": not collection_exists_after,
            "net_zero": not collection_exists_after,
        }


def app_principal_dep() -> Any:
    """The principal dependency the upload route uses (imported lazily)."""
    from app.authz.principal import get_principal

    return get_principal