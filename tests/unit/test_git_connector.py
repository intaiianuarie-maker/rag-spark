"""Unit tests for the GitConnector (TASK-M6-001).

The ``git`` CLI is behind an injectable ``runner`` seam, so tests run offline
against canned ``ls-tree``/``cat-file`` output — no real repo needed. Also runs
the shared connector contract and checks blob-hash integrity.
"""

from __future__ import annotations

import hashlib

import pytest
from connectors import ConnectorConfig, ConnectorKind
from connectors.adapters import GitConnector
from connectors.contract_tests import check_connector_contract


def _git_blob_hash(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()


def _fake_runner(files: dict[str, bytes]):
    """Build a runner that serves ``ls-tree`` and ``cat-file`` from ``files``."""

    def runner(args: list[str], _repo: str) -> bytes:
        if args[:2] == ["ls-tree", "-r"]:
            records = []
            # Real `git ls-tree` sorts by path; mirror that for deterministic order.
            for path in sorted(files):
                sha = _git_blob_hash(files[path])
                records.append(f"100644 blob {sha}\t{path}")
            return ("\0".join(records) + "\0").encode()
        if args[:2] == ["cat-file", "blob"]:
            sha = args[2]
            for content in files.values():
                if _git_blob_hash(content) == sha:
                    return content
            raise RuntimeError(f"blob not found: {sha}")
        raise AssertionError(f"unexpected git invocation: {args}")

    return runner


FILES = {
    "docs/a.md": b"# Alpha\nhello world\n",
    "docs/b.md": b"# Beta\nsecond doc\n",
    "README.md": b"readme\n",
}


@pytest.fixture
def config() -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="c1",
        workspace_id="w1",
        kind=ConnectorKind.GIT,
        settings={"repo": "/srv/repo", "ref": "HEAD"},
    )


@pytest.mark.asyncio
async def test_discover_lists_blobs_with_sha(config: ConnectorConfig) -> None:
    conn = GitConnector(runner=_fake_runner(FILES))
    disc = await conn.discover(config)
    paths = [i.external_id for i in disc.items]
    assert paths == ["README.md", "docs/a.md", "docs/b.md"]  # ls-tree order
    assert all(i.metadata.get("sha") for i in disc.items)


@pytest.mark.asyncio
async def test_cursor_pages_without_reserve(config: ConnectorConfig) -> None:
    conn = GitConnector(page_size=2, runner=_fake_runner(FILES))
    page1 = await conn.discover(config)
    assert [i.external_id for i in page1.items] == ["README.md", "docs/a.md"]
    assert page1.next_cursor == "2"
    page2 = await conn.discover(config, cursor=page1.next_cursor)
    assert [i.external_id for i in page2.items] == ["docs/b.md"]
    assert page2.next_cursor is None
    overlap = {i.external_id for i in page1.items} & {i.external_id for i in page2.items}
    assert not overlap


@pytest.mark.asyncio
async def test_fetch_returns_blob_bytes(config: ConnectorConfig) -> None:
    conn = GitConnector(runner=_fake_runner(FILES))
    disc = await conn.discover(config)
    a = next(i for i in disc.items if i.external_id == "docs/a.md")
    assert await conn.fetch(config, a) == FILES["docs/a.md"]


@pytest.mark.asyncio
async def test_sync_counts_fetched_with_integrity(config: ConnectorConfig) -> None:
    conn = GitConnector(runner=_fake_runner(FILES))
    result = await conn.sync(config)
    assert result.discovered == 3
    assert result.fetched == 3
    assert result.errors == []


@pytest.mark.asyncio
async def test_sync_flags_blob_hash_mismatch(config: ConnectorConfig) -> None:
    # Advertised sha does not match the served blob's real content hash.
    def mismatch_runner(args: list[str], _repo: str) -> bytes:
        if args[:2] == ["ls-tree", "-r"]:
            return (
                "\0".join(
                    [
                        f"100644 blob {'a' * 40}\tdocs/a.md",
                    ]
                )
                + "\0"
            ).encode()
        if args[:2] == ["cat-file", "blob"]:
            # Serve content regardless of the (bogus) requested sha.
            return b"content that does not hash to aaaa...\n"
        raise AssertionError(f"unexpected git invocation: {args}")

    conn = GitConnector(runner=mismatch_runner)
    result = await conn.sync(config)
    assert result.fetched == 0
    assert any("blob hash mismatch" in e for e in result.errors)


@pytest.mark.asyncio
async def test_fetch_missing_blob_raises(config: ConnectorConfig) -> None:
    from connectors.interfaces import SourceItem

    conn = GitConnector(runner=_fake_runner(FILES))
    with pytest.raises(RuntimeError):
        await conn.fetch(
            config, SourceItem(external_id="nope", name="nope", metadata={"sha": "0" * 40})
        )


@pytest.mark.asyncio
async def test_shared_contract(config: ConnectorConfig) -> None:
    conn = GitConnector(runner=_fake_runner(FILES))
    await check_connector_contract(conn, config, min_items=1)


def test_create_connector_resolves_git(config: ConnectorConfig) -> None:
    from connectors import create_connector

    conn = create_connector(config)
    assert isinstance(conn, GitConnector)
    assert conn.kind is ConnectorKind.GIT
