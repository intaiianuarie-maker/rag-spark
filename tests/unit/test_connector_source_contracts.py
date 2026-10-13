"""T-V1-034 — source contracts pinned in code (Git / S3 / web) + cursors.

Instruction 2 of the ticket: "Pin Git local-vs-remote contract, S3 prefix
semantics, web redirect/origin policy." Each contract is asserted against the
REAL adapter (offline: recording runner / in-memory S3 fake / in-memory HTTP
seam), plus the keyset-vs-offset cursor contract the consumer depends on and a
model↔migration parity check for the progress store.

No network anywhere: every transport is an injected fake.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
from connectors import ConnectorConfig, ConnectorKind
from connectors.adapters import GitConnector, LocalFolderConnector, S3Connector, WebConnector
from connectors.adapters.git_connector import GitSourceRefused, RepoScope, classify_repo
from connectors.adapters.web_connector import OutOfScopeRedirect, origin_of, resolve_redirect
from connectors.interfaces import (
    SourceItem,
    is_keyset_cursor,
    keyset_cursor,
    page_items,
    snapshot_digest,
)
from connectors.sync_consumer import SourceIdentity

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---- Git: local-vs-remote contract -------------------------------------------


def _git_config(**settings: Any) -> ConnectorConfig:
    base: dict[str, Any] = {"repo": "/srv/repo", "ref": "HEAD"}
    base.update(settings)
    return ConnectorConfig(
        connector_id="g1", workspace_id="w1", kind=ConnectorKind.GIT, settings=base
    )


class RecordingRunner:
    """Captures every git invocation instead of running one."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self._files = files or {"README.md": b"hi\n"}

    def __call__(self, args: list[str], repo: str) -> bytes:
        self.calls.append((args, repo))
        if args[:2] == ["ls-tree", "-r"]:
            import hashlib

            records = []
            for path in sorted(self._files):
                header = f"blob {len(self._files[path])}\0".encode()
                sha = hashlib.sha1(header + self._files[path]).hexdigest()
                records.append(f"100644 blob {sha}\t{path}")
            return ("\0".join(records) + "\0").encode()
        if args[:2] == ["cat-file", "blob"]:
            return self._files["README.md"]
        raise AssertionError(f"unexpected git invocation: {args}")


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("/srv/repo", RepoScope.LOCAL),
        ("../relative/repo", RepoScope.LOCAL),
        ("file:///srv/repo", RepoScope.LOCAL),
        ("https://example.com/repo.git", RepoScope.REMOTE),
        ("http://example.com/repo.git", RepoScope.REMOTE),
        ("ssh://git@example.com/repo.git", RepoScope.REMOTE),
        ("git://example.com/repo.git", RepoScope.REMOTE),
        ("git@example.com:team/repo.git", RepoScope.REMOTE),
    ],
)
def test_classify_repo_pins_local_versus_remote(repo: str, expected: RepoScope) -> None:
    assert classify_repo(repo) is expected


@pytest.mark.parametrize(
    "repo",
    [
        "https://example.com/repo.git",
        "ssh://git@example.com/repo.git",
        "git@example.com:team/repo.git",
    ],
)
async def test_git_never_hands_a_remote_url_to_the_read_transport(
    repo: str, tmp_path
) -> None:
    """T-V1-040 (renamed): remotes are ACQUIRED, never blind-passed to ``git -C``.

    The old name pinned an outright refusal, which this ticket replaces with
    explicit bounded acquisition. What must still hold is the invariant that
    name was really guarding — **not one read command ever sees the URL**. With
    no acquisition available (no cloner, no reachable remote) the source is
    refused loudly as a ``GitSourceRefused`` and nothing runs at all.
    """
    runner = RecordingRunner()

    def no_acquisition(url: str, dest: str) -> None:
        # Stands in for "the acquisition could not happen" (unreachable
        # remote). Deliberately not a real network attempt: this file must be
        # green with compose down and no egress.
        raise RuntimeError("unreachable")

    conn = GitConnector(
        runner=runner, scratch_root=str(tmp_path), cloner=no_acquisition
    )
    with pytest.raises(GitSourceRefused):
        await conn.discover(_git_config(repo=repo))
    with pytest.raises(GitSourceRefused):
        await conn.fetch(
            _git_config(repo=repo), SourceItem(external_id="x", name="x")
        )
    assert runner.calls == []  # not one command reached the transport


def test_git_file_url_is_local_but_a_file_url_with_a_host_is_not() -> None:
    runner = RecordingRunner()
    conn = GitConnector(runner=runner)

    async def _discover() -> list[str]:
        disc = await conn.discover(_git_config(repo="file:///srv/repo"))
        return [i.external_id for i in disc.items]

    import asyncio

    assert asyncio.run(_discover()) == ["README.md"]
    assert runner.calls[0][1] == "/srv/repo"  # scheme stripped → a local path

    with pytest.raises(ValueError, match="local"):
        conn._repo(_git_config(repo="file://builder.internal/srv/repo"))


async def test_git_never_issues_a_network_or_mutating_subcommand() -> None:
    """Only ls-tree / cat-file can ever reach git (whitelist, not a docstring)."""
    runner = RecordingRunner({"README.md": b"hi\n"})
    conn = GitConnector(page_size=2, runner=runner)
    cfg = _git_config()
    disc = await conn.discover(cfg)
    await conn.fetch(cfg, disc.items[0])
    await conn.sync(cfg)

    assert runner.calls
    assert {args[0] for args, _repo in runner.calls} <= {"ls-tree", "cat-file"}
    for banned in (["fetch"], ["pull"], ["clone"], ["ls-remote"], ["remote", "update"]):
        with pytest.raises(ValueError, match="not allowed"):
            conn._run(banned, "/srv/repo")


async def test_git_keyset_cursor_survives_deletion_of_the_cursor_item() -> None:
    files = {"README.md": b"r\n", "docs/a.md": b"a\n", "docs/b.md": b"b\n"}
    conn = GitConnector(page_size=1, runner=RecordingRunner(files))
    page1 = await conn.discover(_git_config())
    assert [i.external_id for i in page1.items] == ["README.md"]

    del files["README.md"]  # the item the cursor points at disappears

    page2 = await conn.discover(_git_config(), cursor=keyset_cursor("README.md"))
    assert [i.external_id for i in page2.items] == ["docs/a.md"]  # offset "1" → docs/b.md


# ---- S3: prefix semantics -----------------------------------------------------


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class RecordingS3:
    """In-memory S3 that records ListObjectsV2 arguments."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.list_calls.append(kwargs)
        prefix = kwargs.get("Prefix", "")
        contents = [
            {"Key": k, "Size": len(v), "ETag": f'"{k}"', "LastModified": None}
            for k, v in sorted(self.objects.items())
            if k.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(kwargs)
        key = kwargs["Key"]
        if key not in self.objects:
            raise KeyError(key)
        return {"Body": _Body(self.objects[key])}


def _s3(prefix: str) -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="s3",
        workspace_id="w1",
        kind=ConnectorKind.S3,
        settings={"bucket": "bucket", "prefix": prefix},
    )


async def test_s3_prefix_is_a_literal_prefix_not_a_glob() -> None:
    client = RecordingS3({"docs/a.md": b"a", "docs/b.md": b"b"})
    conn = S3Connector(page_size=10, injectable_client=client)
    with pytest.raises(ValueError, match="literal key prefix"):
        await conn.discover(_s3("docs/*"))


async def test_s3_prefix_has_no_implicit_slash_pinned_by_test() -> None:
    """``docs`` matches ``docs-old/…``; ``docs/`` does not. Literal startswith."""
    client = RecordingS3({"docs/a.md": b"a", "docs-old/b.md": b"b"})
    bare = S3Connector(page_size=10, injectable_client=client)
    listed = {i.external_id for i in (await bare.discover(_s3("docs"))).items}
    assert listed == {"docs/a.md", "docs-old/b.md"}  # documented: no implicit "/"

    slashed = S3Connector(page_size=10, injectable_client=client)
    listed = {i.external_id for i in (await slashed.discover(_s3("docs/"))).items}
    assert listed == {"docs/a.md"}


async def test_s3_listing_is_recursive_and_scoped_to_the_prefix() -> None:
    client = RecordingS3({"docs/a.md": b"a", "docs/deep/b.md": b"b", "other/c.md": b"c"})
    conn = S3Connector(page_size=10, injectable_client=client)
    items = {i.external_id for i in (await conn.discover(_s3("docs/"))).items}
    assert items == {"docs/a.md", "docs/deep/b.md"}  # recursive: no Delimiter
    assert client.list_calls
    for call in client.list_calls:
        assert call["Prefix"] == "docs/"
        assert "Delimiter" not in call  # explicit: this connector is recursive
        assert "Bucket" in call


async def test_s3_fetch_refuses_a_key_outside_the_configured_prefix() -> None:
    client = RecordingS3({"docs/a.md": b"a", "secret/b.md": b"b"})
    conn = S3Connector(injectable_client=client)
    cfg = _s3("docs/")
    item = SourceItem(external_id="secret/b.md", name="b.md")
    with pytest.raises(ValueError, match="outside the configured prefix"):
        await conn.fetch(cfg, item)
    assert client.get_calls == []  # refused before any GetObject


async def test_s3_keyset_resume_after_deletion_does_not_skip() -> None:
    client = RecordingS3(
        {"docs/a.md": b"a", "docs/b.md": b"b", "docs/c.md": b"c"}
    )
    conn = S3Connector(page_size=1, injectable_client=client)
    cfg = _s3("docs/")
    page1 = await conn.discover(cfg)
    assert [i.external_id for i in page1.items] == ["docs/a.md"]

    del client.objects["docs/a.md"]  # cursor item gone
    page2 = await conn.discover(cfg, cursor=keyset_cursor("docs/a.md"))
    assert [i.external_id for i in page2.items] == ["docs/b.md"]  # offset "1" → docs/c.md

    # last page: keyset input ⇒ the run is complete (no cursor to persist)
    page3 = await conn.discover(cfg, cursor=keyset_cursor("docs/b.md"))
    assert [i.external_id for i in page3.items] == ["docs/c.md"]
    assert page3.next_cursor is None


# ---- Web: origin + redirect policy -------------------------------------------


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://seed.test/page", "https://seed.test"),
        ("https://Seed.TEST:443/x", "https://seed.test"),  # case + default port
        ("http://seed.test:8080/x", "http://seed.test:8080"),  # explicit port kept
        ("https://seed.test.evil.example/", "https://seed.test.evil.example"),
        # ^ a valid origin, but a DIFFERENT one — see the lookalike test below
        ("file:///etc/passwd", None),
        ("data:text/html,<b>", None),
        ("javascript:alert(1)", None),
        ("https://user:pass@seed.test/", None),  # credentialed ≠ plain origin
        ("//seed.test/x", None),  # scheme-relative with no base is not resolvable
    ],
)
def test_origin_of_pins_the_origin_comparison(url: str, origin: str | None) -> None:
    assert origin_of(url) == origin


def test_lookalike_host_is_a_different_origin_not_a_string_prefix() -> None:
    """The bug this policy closes: ``startswith(origin)`` admits lookalikes."""
    seed = "https://seed.test/"
    lookalike = "https://seed.test.evil.example/secret"
    assert origin_of(lookalike) is not None  # it IS an origin — just not ours
    assert origin_of(lookalike) != origin_of(seed)
    assert lookalike.startswith(seed.rstrip("/"))  # what the old check allowed


def test_resolve_redirect_allows_same_origin_and_refuses_the_rest() -> None:
    allowed = {"https://seed.test"}
    assert (
        resolve_redirect(
            "https://seed.test/a", "/b", allowed, max_redirects=5, hops=0
        )
        == "https://seed.test/b"
        )
    for bad_location in (
        "https://other.test/steal",
        "https://seed.test.evil.example/",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ):
        with pytest.raises(OutOfScopeRedirect):
            resolve_redirect(
                "https://seed.test/a", bad_location, allowed, max_redirects=5, hops=0
            )
    with pytest.raises(ValueError, match="exceeded 5 redirects"):
        resolve_redirect(
            "https://seed.test/a", "/b", allowed, max_redirects=5, hops=5
        )


def _web_cfg(urls: list[str], **settings: Any) -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="web",
        workspace_id="w1",
        kind=ConnectorKind.WEB,
        settings={"urls": urls, **settings},
    )


class RecordingFetcher:
    def __init__(self, pages: dict[str, tuple[bytes, dict[str, str]]]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def __call__(self, url: str) -> tuple[bytes, dict[str, str]]:
        self.fetched.append(url)
        if url not in self.pages:
            raise ConnectionError("404")
        return self.pages[url]


async def test_web_never_fetches_a_lookalike_origin() -> None:
    """``seed.test.evil.example`` starts with the seed's origin — not by origin."""
    evil = "http://seed.test.evil.example/secret"
    pages = {
        "http://seed.test/": (
            f'<a href="{evil}">x</a><a href="/ok">y</a>'.encode(),
            {},
        ),
        "http://seed.test/ok": (b"ok", {}),
    }
    fetcher = RecordingFetcher(pages)
    conn = WebConnector(max_depth=1, fetch=fetcher)
    disc = await conn.discover(_web_cfg(["http://seed.test/"]))

    urls = {i.uri for i in disc.items}
    assert evil not in urls
    assert "http://seed.test/ok" in urls
    assert evil not in fetcher.fetched  # never even requested


async def test_web_rejects_non_http_seeds() -> None:
    conn = WebConnector(fetch=RecordingFetcher({}))
    for seed in ("file:///etc/passwd", "data:text/plain,x", "javascript:alert(1)"):
        with pytest.raises(ValueError, match="http"):
            conn._seeds(_web_cfg([seed]))


async def test_web_explicit_allowed_origins_opt_in() -> None:
    cdn = "https://cdn.test/file"
    pages = {
        "https://seed.test/": (f'<a href="{cdn}">c</a>'.encode(), {}),
        cdn: (b"asset", {}),
    }
    fetcher = RecordingFetcher(pages)
    conn = WebConnector(max_depth=1, fetch=fetcher)
    disc = await conn.discover(_web_cfg(["https://seed.test/"]))
    assert cdn not in {i.uri for i in disc.items}  # default: same-origin only

    fetcher2 = RecordingFetcher(pages)
    conn2 = WebConnector(max_depth=1, fetch=fetcher2)
    disc2 = await conn2.discover(
        _web_cfg(["https://seed.test/"], allowed_origins=["https://cdn.test"])
    )
    assert cdn in {i.uri for i in disc2.items}
    assert fetcher2.fetched.count(cdn) == 1


async def test_web_fetch_scope_guard_runs_before_the_transport() -> None:
    fetcher = RecordingFetcher({})
    conn = WebConnector(fetch=fetcher)
    cfg = _web_cfg(["http://seed.test/"])
    with pytest.raises(OutOfScopeRedirect):
        await conn.fetch(
            cfg,
            SourceItem(
                external_id="http://evil.test/x", name="x", uri="http://evil.test/x"
            ),
        )
    assert fetcher.fetched == []  # the seam was never reached


# ---- keyset cursor contract (what the consumer persists) ---------------------


def _items(*ids: str) -> list[SourceItem]:
    return [SourceItem(external_id=i, name=i) for i in ids]


def test_keyset_window_is_independent_of_prior_positions() -> None:
    items = _items("a", "b", "c", "d")
    first, cursor = page_items(items, None, 2)
    assert [i.external_id for i in first] == ["a", "b"]
    assert cursor == "2"  # legacy offset shape when STARTED without a cursor
    assert is_keyset_cursor(cursor) is False

    second, kcursor = page_items(items, keyset_cursor("b"), 2)
    assert [i.external_id for i in second] == ["c", "d"]
    assert kcursor is None
    # deleting the cursor key does not move the resume point
    without_b = _items("a", "c", "d")
    window, _ = page_items(without_b, keyset_cursor("b"), 10)
    assert [i.external_id for i in window] == ["c", "d"]


def test_keyset_out_cursor_round_trips_through_the_adapter_shape() -> None:
    assert keyset_cursor("docs/a.md") == "k:docs/a.md"
    assert is_keyset_cursor("k:docs/a.md") is True
    assert is_keyset_cursor("12") is False
    with pytest.raises(ValueError):
        keyset_cursor("")


def test_unsupported_cursor_fails_loudly_instead_of_restarting_the_scan() -> None:
    with pytest.raises(ValueError, match="unsupported cursor"):
        page_items(_items("a"), "not-a-cursor", 10)
    with pytest.raises(ValueError, match="empty keyset cursor"):
        page_items(_items("a"), "k:", 10)


async def test_snapshot_digest_is_content_scoped_and_page_independent(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.md").write_text("one", encoding="utf-8")
    (tmp_path / "b.md").write_text("two", encoding="utf-8")
    cfg = ConnectorConfig(
        connector_id="l",
        workspace_id="w1",
        kind=ConnectorKind.LOCAL_UPLOAD,
        settings={"path": str(tmp_path)},
    )
    conn = LocalFolderConnector(page_size=1)
    page1 = await conn.discover(cfg)
    page2 = await conn.discover(cfg, cursor=page1.next_cursor)
    assert page1.snapshot == page2.snapshot  # digest of the FULL listing, not the page

    (tmp_path / "a.md").write_text("changed", encoding="utf-8")
    page3 = await conn.discover(cfg)
    assert page3.snapshot != page1.snapshot  # content change ⇒ new snapshot

    assert snapshot_digest(_items("a")) != snapshot_digest(_items("b"))


# ---- sync queue: T-V1-020 protocol reuse, disjoint namespace -----------------


def test_sync_queue_reuses_the_claim_ack_scripts_and_stays_disjoint() -> None:
    """The lease protocol is imported, not copied (drift would break the pin)."""
    from connectors.adapters import sync_queue
    from connectors.sync_consumer import SyncQueue
    from ingestion.adapters import redis_queue

    assert sync_queue._CLAIM_LUA is redis_queue._CLAIM_LUA
    assert sync_queue._ACK_LUA is redis_queue._ACK_LUA
    assert sync_queue._RECOVER_LUA is redis_queue._RECOVER_LUA
    assert sync_queue.get_client is redis_queue.get_client  # same broker, one pool

    # ...but the keys are its own, so an ingest worker can never claim a sync
    # job (and vice versa): same broker, disjoint queues.
    assert sync_queue.queue_key().startswith("sync:")
    assert sync_queue.queue_key() != redis_queue.queue_key()
    assert sync_queue.claims_key() != redis_queue.claims_key()
    assert isinstance(sync_queue.RedisSyncQueue(), SyncQueue)


# ---- migration ↔ model parity -------------------------------------------------


def _load_migration():
    path = REPO_ROOT / "apps" / "api" / "alembic" / "versions" / "0008_connector_sync_progress.py"
    spec = importlib.util.spec_from_file_location("m0008_connector_sync_progress", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_matches_the_orm_models() -> None:
    from connectors.adapters.progress_store import SyncOutcomeRow, SyncProgressRow

    migration = _load_migration()
    # read the real upgrade() SQL, not the module docstring
    source = inspect.getsource(migration.upgrade)

    for table, model in (
        ("connector_sync_progress", SyncProgressRow),
        ("connector_sync_outcomes", SyncOutcomeRow),
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source
        for column in model.__table__.columns:
            assert re.search(rf"\b{column.name}\b", source), (
                f"column {table}.{column.name} missing from migration 0008"
            )
        for constraint in model.__table__.constraints:
            if constraint.name:
                assert constraint.name in source, (
                    f"constraint {constraint.name} missing from migration 0008"
                )
    # downgrade exists and is a real teardown
    assert "DROP TABLE IF EXISTS connector_sync_outcomes" in inspect.getsource(
        migration.downgrade
    )


def test_progress_store_implements_the_protocol() -> None:
    from connectors.adapters.progress_store import SqlAlchemySyncProgressStore
    from connectors.sync_consumer import InMemorySyncProgressStore, SyncProgressStore

    assert isinstance(InMemorySyncProgressStore(), SyncProgressStore)
    assert isinstance(SqlAlchemySyncProgressStore(session_factory=None), SyncProgressStore)


def test_source_identity_binds_settings_to_the_source() -> None:
    a = ConnectorConfig(
        connector_id="c",
        workspace_id="w",
        kind=ConnectorKind.S3,
        settings={"bucket": "b", "prefix": "p/"},
    )
    b = ConnectorConfig(
        connector_id="c",
        workspace_id="w",
        kind=ConnectorKind.S3,
        settings={"bucket": "b", "prefix": "other/"},
    )
    assert SourceIdentity.from_config(a) != SourceIdentity.from_config(b)
    assert SourceIdentity.from_config(a).key == "w/c"
