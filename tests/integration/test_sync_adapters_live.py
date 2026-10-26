"""M6 live S3/Git → durable sync → ingestion → downstream delete.

Only embedding generation is replaced. PostgreSQL schemas, Redis keys, S3
buckets and Qdrant collections are unique per test and cleaned explicitly.
The ``downstream`` cases perform actual vector indexing: run them through
``/root/code/bin/herdr-heavy-run -- ...pytest... -k downstream`` on this host.
The remaining cases exercise only small connector/SQL/Redis operations.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest
from connectors.adapters import sync_queue
from connectors.adapters.git_connector import GitConnector
from connectors.adapters.progress_store import Base as ConnectorsBase
from connectors.adapters.progress_store import SqlAlchemySyncProgressStore
from connectors.adapters.s3_connector import S3Connector
from connectors.adapters.source_document_map import get_mapped_document
from connectors.interfaces import ConnectorConfig, ConnectorKind
from connectors.sync_consumer import SourceIdentity
from core.config import get_settings
from core.errors import ReasonCode, normalize_code
from ingestion import persistence
from ingestion.adapters import redis_queue
from ingestion.persistence import ChunkRow, Document, DocumentVersion, IngestionJob
from jobs.sync_worker import (
    default_sync_consumer,
    make_document_deleter,
    make_document_publisher,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from storage import originals
from storage.adapters.seaweedfs_s3 import SeaweedFSAdapter

pytestmark = pytest.mark.integration

FIRST = b"Alpha orchard document contains apples and pears.\n"
SECOND = b"Beta garden document contains roses and lilies.\n"
CHANGED = b"Gamma revised document contains plums and peaches.\n"


def _local(url: str) -> None:
    if urlparse(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("M6 live fixtures require explicitly local service URLs")


@pytest.fixture
async def broker(monkeypatch):
    from redis.asyncio import Redis
    from redis.exceptions import RedisError

    url = get_settings().redis_url
    _local(url)
    client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
    try:
        await client.ping()
    except RedisError:
        await client.aclose()
        pytest.skip("local Redis unavailable; start compose redis")
    prefix = f"m6-live-{uuid.uuid4().hex}:"
    monkeypatch.setenv("SYNC_QUEUE_PREFIX", prefix)
    monkeypatch.setenv("INGEST_QUEUE_PREFIX", prefix)
    monkeypatch.setattr(sync_queue, "get_client", lambda: client)
    monkeypatch.setattr(redis_queue, "get_client", lambda: client)
    keys = [
        sync_queue.queue_key(), sync_queue.claims_key(), sync_queue.claim_tokens_key(),
        redis_queue.queue_key(), redis_queue.claims_key(), redis_queue.claim_tokens_key(),
        redis_queue.delayed_key(),
    ]
    try:
        yield client
    finally:
        await client.delete(*keys)
        await client.aclose()


@pytest.fixture
async def bucket(monkeypatch):
    from object_store_env import apply

    endpoint = apply()
    _local(endpoint)
    adapter = SeaweedFSAdapter(endpoint_url=endpoint)
    name = "m6-live-" + uuid.uuid4().hex
    # Never create/delete the configured product bucket, even on failed setup.
    adapter.client.create_bucket(Bucket=name)
    monkeypatch.setenv("S3_BUCKET", name)
    monkeypatch.setattr(originals, "_adapter", adapter)
    try:
        yield name, adapter
    finally:
        # Bucket is private to this fixture. Paginate rather than assuming a
        # single listing is complete; errors remain visible as cleanup failures.
        for page in adapter.client.get_paginator("list_objects_v2").paginate(Bucket=name):
            for obj in page.get("Contents", []):
                adapter.client.delete_object(Bucket=name, Key=obj["Key"])
        adapter.client.delete_bucket(Bucket=name)
        await adapter.aclose()


@dataclass
class LiveSource:
    config: ConnectorConfig
    connector: object
    a: str
    b: str
    repo: Path | None = None
    bucket: str | None = None
    adapter: SeaweedFSAdapter | None = None

    def commit(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "core.hooksPath=/dev/null",
             "-c", "user.name=M6 Fixture", "-c", "user.email=m6@example.test",
             "add", "--all"], check=True, capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "core.hooksPath=/dev/null",
             "-c", "user.name=M6 Fixture", "-c", "user.email=m6@example.test",
             "commit", "--no-gpg-sign", "-m", "fixture update"],
            check=True, capture_output=True, timeout=10,
        )

    async def modify(self) -> None:
        if self.repo is not None:
            (self.repo / self.a).write_bytes(CHANGED)
            await asyncio.to_thread(self.commit)
        else:
            await self.adapter.put_object(self.bucket, self.a, CHANGED)

    async def delete(self) -> None:
        if self.repo is not None:
            (self.repo / self.a).unlink()
            await asyncio.to_thread(self.commit)
        else:
            await self.adapter.delete_object(self.bucket, self.a)


@pytest.fixture
async def git_source(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    await asyncio.to_thread(
        subprocess.run, ["git", "init", "--quiet", str(repo)], check=True, timeout=10,
    )
    for path, content in [("a/readme.txt", FIRST), ("b/readme.txt", SECOND)]:
        target = repo / path
        target.parent.mkdir()
        target.write_bytes(content)
    config = ConnectorConfig(
        connector_id="fixture-git", workspace_id=str(uuid.uuid4()), kind=ConnectorKind.GIT,
        settings={"repo": str(repo)},
    )
    fixture = LiveSource(config, GitConnector(page_size=1),
                         "a/readme.txt", "b/readme.txt", repo=repo)
    await asyncio.to_thread(fixture.commit)
    return fixture


@pytest.fixture
async def s3_source(bucket):
    name, adapter = bucket
    workspace = str(uuid.uuid4())
    a, b = "source/a/readme.txt", "source/b/readme.txt"
    await adapter.put_object(name, a, FIRST)
    await adapter.put_object(name, b, SECOND)
    await adapter.put_object(name, "outside/keep.txt", b"outside source scope")
    config = ConnectorConfig(
        connector_id="fixture-s3", workspace_id=workspace, kind=ConnectorKind.S3,
        settings={"bucket": name, "prefix": "source/"},
    )
    yield LiveSource(config, S3Connector(page_size=1, injectable_client=adapter.client),
                     a, b, bucket=name, adapter=adapter)
    assert await adapter.get_object(name, "outside/keep.txt") == b"outside source scope"


@pytest.fixture(params=["s3", "git"])
async def source(request, s3_source, git_source):
    return s3_source if request.param == "s3" else git_source


@pytest.fixture
async def database(monkeypatch):
    url = get_settings().database_url
    _local(url)
    schema = "m6_live_" + uuid.uuid4().hex
    admin = create_async_engine(url, connect_args={"connect_timeout": 3})
    engine = create_async_engine(
        url, connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
    )
    try:
        async with admin.connect():
            pass
    except OperationalError:
        await engine.dispose()
        await admin.dispose()
        pytest.skip("local PostgreSQL unavailable; start compose postgres")
    try:
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        async with engine.begin() as conn:
            await conn.run_sync(persistence.Base.metadata.create_all)
            await conn.run_sync(ConnectorsBase.metadata.create_all)
            # Migration 0007's table shape, scoped to the disposable schema.
            # No alembic/public-schema mutation or shared-table dependency.
            await conn.exec_driver_sql("""
                CREATE TABLE lexical_docs (
                    id UUID PRIMARY KEY, workspace_id TEXT NOT NULL,
                    document_id TEXT, document_version_id TEXT, chunk_id TEXT,
                    content TEXT NOT NULL, language TEXT, metadata JSONB DEFAULT '{}',
                    tsv tsvector, tsv_cfg TEXT, page INTEGER, section TEXT
                )
            """)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(persistence, "get_session_factory", lambda: factory)
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.dispose()


def _consumer(source, database):
    return default_sync_consumer(
        store=SqlAlchemySyncProgressStore(session_factory=database),
        publisher=make_document_publisher(
            session_factory=database, enqueue_fn=redis_queue.enqueue,
        ),
        deleter=make_document_deleter(session_factory=database),
        connector_factory=lambda config: source.connector,
        load_sources=lambda workspace: [source.config],
    )


async def _sync(source, database):
    # Recreate the consumer/store every run: convergence must survive restart.
    await sync_queue.enqueue_sync_job(source.config.workspace_id, source.config.connector_id)
    result = await _consumer(source, database).run_once(timeout_seconds=1)
    assert result is not None and result.acked
    assert len(result.reports) == 1
    report = result.reports[0]
    assert report.failed == 0, report.errors
    assert report.cursor is None
    return report


async def _mapping(database, source, external_id):
    async with database() as session:
        return await get_mapped_document(
            session, workspace_id=uuid.UUID(source.config.workspace_id),
            connector_id=source.config.connector_id, external_id=external_id,
        )


async def _counts(database):
    async with database() as session:
        return tuple(
            [await session.scalar(select(func.count()).select_from(model))
             for model in [Document, DocumentVersion, IngestionJob]]
        )


async def _assert_connector_roundtrip(source):
    async def contents():
        cursor = None
        found = {}
        while True:
            page = await source.connector.discover(source.config, cursor)
            for item in page.items:
                found[item.external_id] = await source.connector.fetch(source.config, item)
            if page.next_cursor is None:
                return found
            assert page.next_cursor != cursor
            cursor = page.next_cursor

    assert await contents() == {source.a: FIRST, source.b: SECOND}
    assert await contents() == {source.a: FIRST, source.b: SECOND}
    await source.modify()
    assert await contents() == {source.a: CHANGED, source.b: SECOND}
    await source.delete()
    assert await contents() == {source.b: SECOND}


async def test_live_git_connector_fetches_changed_and_deleted_source(git_source):
    await _assert_connector_roundtrip(git_source)


async def test_live_s3_connector_fetches_changed_and_deleted_source(s3_source):
    await _assert_connector_roundtrip(s3_source)


async def test_live_sync_persists_sql_originals_and_unchanged(source, database, broker):
    first = await _sync(source, database)
    assert (first.created, first.updated, first.unchanged) == (2, 0, 0)
    a = await _mapping(database, source, source.a)
    b = await _mapping(database, source, source.b)
    assert a is not None and b is not None and a != b  # same basename, distinct identity
    assert await _counts(database) == (2, 2, 2)
    unchanged = await _sync(source, database)
    assert (unchanged.created, unchanged.updated, unchanged.unchanged) == (0, 0, 2)
    assert await _counts(database) == (2, 2, 2)
    await source.modify()
    changed = await _sync(source, database)
    assert (changed.created, changed.updated, changed.unchanged) == (0, 1, 1)
    assert await _mapping(database, source, source.a) == a
    assert await _counts(database) == (2, 3, 3)
    async with database() as session:
        versions = (await session.scalars(select(DocumentVersion).where(
            DocumentVersion.document_id == a,
        ).order_by(DocumentVersion.version_number))).all()
        assert [v.checksum for v in versions] == [
            hashlib.sha256(FIRST).hexdigest(), hashlib.sha256(CHANGED).hexdigest(),
        ]
        for version, expected in zip(versions, [FIRST, CHANGED], strict=True):
            assert await originals.load_original(version.storage_key,
                                                 checksum=version.checksum) == expected


async def test_live_redis_sync_lease_expiry_recovers_and_fences_ack(broker, monkeypatch):
    monkeypatch.setenv("INGEST_QUEUE_LEASE_SECONDS", "0.05")
    payload = await sync_queue.enqueue_sync_job(str(uuid.uuid4()), "fixture")
    queue = sync_queue.RedisSyncQueue()
    original = await queue.claim(timeout_seconds=0)
    assert original is not None and original.job_id == payload
    assert await broker.llen(sync_queue.queue_key()) == 0
    await asyncio.sleep(0.08)
    assert await sync_queue.recover_expired() == 1
    recovered = await queue.claim(timeout_seconds=0)
    assert recovered is not None and recovered.job_id == payload
    assert recovered.token != original.token
    assert not await queue.ack(original)
    assert await broker.hget(sync_queue.claim_tokens_key(), payload) == recovered.token
    assert await queue.ack(recovered)
    assert not await queue.ack(recovered)
    assert await broker.zcard(sync_queue.claims_key()) == 0
    assert await broker.hlen(sync_queue.claim_tokens_key()) == 0
    assert await queue.claim(timeout_seconds=0) is None


async def _assert_discovery_failure(source, database, broker, caplog, error_type, sentinel):
    """Check real discovery errors at the durable consumer/claim boundary."""
    from connectors.adapters.progress_store import SyncOutcomeRow, SyncProgressRow
    from connectors.adapters.source_document_map import SyncSourceDocument

    started = datetime.now(UTC)
    payload = await sync_queue.enqueue_sync_job(
        source.config.workspace_id, source.config.connector_id,
    )
    log_start = len(caplog.records)
    with caplog.at_level(logging.INFO, logger="knowledge_os.connectors.sync"):
        with pytest.raises(error_type) as failure:
            await _consumer(source, database).run_once(timeout_seconds=1)

    # The job must remain under its original lease, with no ready-queue
    # duplicate or ingestion dispatch. The common lease recovery test above
    # proves that such an unacked claim can be retried after expiry.
    assert await broker.zrange(sync_queue.claims_key(), 0, -1) == [payload]
    assert await broker.hget(sync_queue.claim_tokens_key(), payload)
    assert await broker.llen(sync_queue.queue_key()) == 0
    assert await broker.llen(redis_queue.queue_key()) == 0
    assert await _counts(database) == (0, 0, 0)

    # Reopen the SQL store instead of inspecting the consumer's in-memory
    # progress: the failure has to survive a worker restart and be UI-visible.
    identity = SourceIdentity.from_config(source.config)
    store = SqlAlchemySyncProgressStore(session_factory=database)
    progress = await store.load(identity)
    assert progress is not None
    assert progress.last_attempt_at is not None and progress.last_attempt_at >= started
    assert progress.last_finished_at is not None
    assert progress.last_finished_at >= progress.last_attempt_at
    assert progress.last_success_at is None
    assert progress.last_error_code == normalize_code(ReasonCode.INGESTION_ERROR)
    assert progress.last_run_id
    assert progress.cursor is None
    assert progress.items_seen == progress.items_created == progress.items_updated == 0
    assert await store.outcomes(identity, progress.generation) == {}
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(SyncSourceDocument)) == 0
        persisted = []
        for model in [SyncProgressRow, SyncOutcomeRow]:
            persisted.extend((await session.execute(select(model.__table__))).mappings().all())
    assert len(persisted) == 1  # one source failure; no invented item outcome
    assert sentinel not in repr(persisted)
    assert sentinel not in repr([record.__dict__ for record in caplog.records[log_start:]])
    assert any(record.message == "sync_job_failed" for record in caplog.records[log_start:])
    return failure.value


async def test_live_s3_bad_credentials_persist_failure_without_ack(
    s3_source, database, broker, caplog,
):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    # The admin-created bucket contains actual objects. An unauthenticated S3
    # fixture would list them successfully and FAIL this test, never skip it.
    sentinel = "m6-secret-sentinel-" + uuid.uuid4().hex
    client = boto3.client(
        "s3", endpoint_url=s3_source.adapter.endpoint_url,
        aws_access_key_id=s3_source.adapter.access_key,
        aws_secret_access_key=sentinel, region_name="us-east-1",
        config=Config(signature_version="s3v4", connect_timeout=1, read_timeout=2,
                      retries={"total_max_attempts": 1}),
    )
    rejected = LiveSource(
        s3_source.config, S3Connector(injectable_client=client), s3_source.a, s3_source.b,
    )
    try:
        error = await _assert_discovery_failure(
            rejected, database, broker, caplog, ClientError, sentinel,
        )
        assert error.operation_name == "ListObjectsV2"
        assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 403
        assert error.response["Error"]["Code"] in {"SignatureDoesNotMatch", "AccessDenied"}
    finally:
        client.close()


async def test_live_git_unreachable_http_host_persists_failure_without_ack(
    database, broker, caplog, tmp_path,
):
    from connectors.adapters.git_connector import GitSourceRefused

    sentinel = "m6-secret-sentinel-" + uuid.uuid4().hex
    # A bound, non-listening loopback socket reserves a private port while
    # making connects fail immediately. There is no HTTP mock or Git runner.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        config = ConnectorConfig(
            connector_id="unreachable-git", workspace_id=str(uuid.uuid4()),
            kind=ConnectorKind.GIT,
            settings={"repo": f"http://127.0.0.1:{port}/{sentinel}.git"},
        )
        source = LiveSource(config, GitConnector(scratch_root=str(tmp_path)), "", "")
        error = await _assert_discovery_failure(
            source, database, broker, caplog, GitSourceRefused, sentinel,
        )
    # Prove that real Git acquisition ran and failed, rather than a URL
    # validation refusal satisfying the negative case accidentally.
    assert isinstance(error.__cause__, subprocess.CalledProcessError)
    assert error.__cause__.returncode != 0
    assert "clone" in error.__cause__.cmd
    assert list(tmp_path.iterdir()) == []  # failed acquisition cleaned scratch


class FixtureEmbedder:
    dimension = 4
    model_name = "m6-fixture-no-weights"
    revision = "1"

    def __init__(self, **kwargs):
        pass

    async def embed_documents(self, texts):
        return [[1.0, 0.25, 0.5, 0.75] for _ in texts]


async def test_live_sync_downstream_modify_delete(source, database, broker, monkeypatch):
    """Actual indexing: admission through herdr-heavy-run is required on this host."""
    from ingestion import dedup, metadata
    from jobs import ingest_pipeline
    from models.adapters import openrouter_provider
    from qdrant_client import AsyncQdrantClient
    from retrieval.adapters.pg_fts import PostgresLexicalStore
    from retrieval.adapters.qdrant_store import QdrantVectorStore
    from retrieval.interfaces import SearchFilter

    settings = get_settings()
    _local(settings.qdrant_url)
    collection = "knowledge_test_m6_" + uuid.uuid4().hex
    monkeypatch.setattr(settings, "vector_collection", collection)
    monkeypatch.setattr(openrouter_provider, "OpenRouterProvider", FixtureEmbedder)
    monkeypatch.setattr(ingest_pipeline, "CHUNKING_BACKEND", "builtin")
    # Use the real stopword fallback; never trigger lingua's lazy model load.
    monkeypatch.setattr(metadata, "_DETECTOR", None)
    monkeypatch.setattr(dedup, "_STORE_PURGERS", {})
    monkeypatch.setenv("GRAPH_MODE", "off")
    client = AsyncQdrantClient(url=settings.qdrant_url, timeout=5)
    vector = QdrantVectorStore(collection=collection)
    lexical = PostgresLexicalStore(session_factory=database)

    async def ingest_queued():
        while (claim := await redis_queue.claim(timeout_seconds=0)) is not None:
            async with database() as session:
                await ingest_pipeline.process_job(session, uuid.UUID(str(claim)))
                job = await session.get(IngestionJob, uuid.UUID(str(claim)))
                assert job.status == "ready", (job.status, job.error_code)
            assert await redis_queue.ack(claim)

    async def chunks(document_id):
        async with database() as session:
            return list((await session.scalars(select(ChunkRow.id).where(
                ChunkRow.workspace_id == uuid.UUID(source.config.workspace_id),
                ChunkRow.document_version_id.in_(select(DocumentVersion.id).where(
                    DocumentVersion.document_id == document_id,
                )),
            ))).all())

    async def assert_indexed(document_id):
        ids = [str(value) for value in await chunks(document_id)]
        assert ids
        assert await lexical.count_ids(ids=ids) == len(ids)
        assert await vector.count_ids(collection=collection, ids=ids) == len(ids)
        return ids

    try:
        first = await _sync(source, database)
        assert (first.created, first.updated, first.unchanged, first.deleted) == (2, 0, 0, 0)
        await ingest_queued()
        a = await _mapping(database, source, source.a)
        b = await _mapping(database, source, source.b)
        assert a is not None and b is not None and a != b
        old_ids = await assert_indexed(a)
        survivor_ids = await assert_indexed(b)
        unchanged = await _sync(source, database)
        assert (unchanged.created, unchanged.updated, unchanged.unchanged) == (0, 0, 2)
        assert await _counts(database) == (2, 2, 2)
        assert await broker.llen(redis_queue.queue_key()) == 0
        await source.modify()
        updated = await _sync(source, database)
        assert (updated.created, updated.updated, updated.unchanged) == (0, 1, 1)
        await ingest_queued()
        assert await _mapping(database, source, source.a) == a
        current_ids = await assert_indexed(a)
        assert set(current_ids).isdisjoint(old_ids)
        assert await lexical.count_ids(ids=old_ids) == 0
        assert await vector.count_ids(collection=collection, ids=old_ids) == 0
        assert await _counts(database) == (2, 3, 3)
        await source.delete()
        deleted = await _sync(source, database)
        assert (deleted.created, deleted.updated, deleted.unchanged, deleted.deleted) == (
            0, 0, 1, 1,
        )
        assert await _mapping(database, source, source.a) is None
        assert await _mapping(database, source, source.b) == b
        assert await chunks(a) == []
        assert await lexical.count_ids(ids=current_ids) == 0
        assert await vector.count_ids(collection=collection, ids=current_ids) == 0
        assert await lexical.count_ids(ids=survivor_ids) == len(survivor_ids)
        assert await vector.count_ids(collection=collection, ids=survivor_ids) == len(survivor_ids)
        async with database() as session:
            assert (await session.get(Document, a)).deleted_at is not None
            assert (await session.get(Document, b)).deleted_at is None
            assert await session.scalar(text(
                "SELECT count(*) FROM lexical_docs WHERE document_id = :did"
            ), {"did": str(a)}) == 0
        assert await lexical.search(query="plums", top_k=5, query_filter=SearchFilter(
            workspace_ids=[source.config.workspace_id], document_ids=[str(a)],
        )) == []
        adapter = originals.get_adapter()
        bucket_name = originals.originals_bucket()
        assert await adapter.list_objects(bucket_name, prefix=dedup.originals_prefix(
            uuid.UUID(source.config.workspace_id), a,
        )) == []
        assert await adapter.list_objects(bucket_name, prefix=dedup.originals_prefix(
            uuid.UUID(source.config.workspace_id), b,
        ))
    finally:
        # This exact generated name is the only collection cleanup may touch.
        try:
            if await client.collection_exists(collection):
                await client.delete_collection(collection)
        finally:
            await vector.close()
            await client.close()
