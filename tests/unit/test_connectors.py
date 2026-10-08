"""Connector adapter tests (TASK-M6-001).

Runs the REAL connectors (LocalFolderConnector, S3Connector) through the shared
connector contract with an in-memory S3 fake, plus targeted checks: stable
discovery order, cursor paging that never re-serves an item, checksum metadata
for dedup, and loud failure on a missing item.
"""

from __future__ import annotations

import pytest
from connectors.adapters import LocalFolderConnector, S3Connector
from connectors.contract_tests import check_connector_contract
from connectors.interfaces import ConnectorConfig, ConnectorKind, SourceItem

pytestmark = [pytest.mark.unit, pytest.mark.contract]


# ---- local folder connector --------------------------------------------------


def _local_config(tmp_path) -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="conn-local",
        workspace_id="ws-a",
        kind=ConnectorKind.LOCAL_UPLOAD,
        settings={"path": str(tmp_path)},
    )


async def test_local_folder_satisfies_contract(tmp_path):
    (tmp_path / "a.md").write_text("# alpha\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# beta\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("# gamma\n", encoding="utf-8")
    conn = LocalFolderConnector(page_size=2)
    await check_connector_contract(conn, _local_config(tmp_path), min_items=2)


async def test_local_folder_pages_without_re_serving(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.md").write_text(name, encoding="utf-8")
    conn = LocalFolderConnector(page_size=2)
    config = _local_config(tmp_path)
    first = await conn.discover(config)
    assert [i.external_id for i in first.items] == ["a.md", "b.md"]
    second = await conn.discover(config, cursor=first.next_cursor)
    assert [i.external_id for i in second.items] == ["c.md"]
    assert second.next_cursor is None


async def test_local_folder_discovery_is_stable(tmp_path):
    (tmp_path / "z.md").write_text("z", encoding="utf-8")
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    conn = LocalFolderConnector(page_size=10)
    config = _local_config(tmp_path)
    first = await conn.discover(config)
    repeat = await conn.discover(config)
    assert [i.external_id for i in first.items] == [i.external_id for i in repeat.items]
    assert [i.external_id for i in first.items] == ["a.md", "z.md"]


async def test_local_folder_emits_checksum_for_dedup(tmp_path):
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")
    conn = LocalFolderConnector(page_size=10)
    items = (await conn.discover(_local_config(tmp_path))).items
    assert items[0].metadata.get("checksum")
    assert len(items[0].metadata["checksum"]) == 64  # sha256 hex


async def test_local_folder_fetch_missing_is_loud(tmp_path):
    conn = LocalFolderConnector()
    with pytest.raises((KeyError, ValueError, FileNotFoundError)):
        await conn.fetch(_local_config(tmp_path), SourceItem(external_id="ghost", name="ghost"))


# ---- S3 connector (in-memory boto3 fake) -------------------------------------


class _FakeS3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3Client:
    """Minimal S3 fake: list_objects_v2 (single page) + get_object."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = dict(objects)

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        contents = [
            {"Key": k, "Size": len(v), "ETag": f'"{k}"', "LastModified": None}
            for k, v in sorted(self._objects.items())
            if k.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self._objects:
            raise KeyError(key)
        return {"Body": _FakeS3Body(self._objects[key])}


def _s3_config() -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="conn-s3",
        workspace_id="ws-a",
        kind=ConnectorKind.S3,
        settings={"bucket": "test-bucket", "prefix": "docs/"},
    )


async def test_s3_satisfies_contract():
    client = _FakeS3Client(
        {
            "docs/a.md": b"# a\n",
            "docs/b.md": b"# b\n",
            "docs/c.md": b"# c\n",
        }
    )
    conn = S3Connector(page_size=2, injectable_client=client)
    await check_connector_contract(conn, _s3_config(), min_items=2)


async def test_s3_pages_without_re_serving():
    client = _FakeS3Client(
        {"docs/a.md": b"a", "docs/b.md": b"b", "docs/c.md": b"c"}
    )
    conn = S3Connector(page_size=2, injectable_client=client)
    config = _s3_config()
    first = await conn.discover(config)
    second = await conn.discover(config, cursor=first.next_cursor)
    overlap = {i.external_id for i in first.items} & {i.external_id for i in second.items}
    assert not overlap


async def test_s3_discovery_is_sorted_and_stable():
    client = _FakeS3Client({"docs/z.md": b"z", "docs/a.md": b"a"})
    conn = S3Connector(page_size=10, injectable_client=client)
    config = _s3_config()
    first = await conn.discover(config)
    repeat = await conn.discover(config)
    assert [i.external_id for i in first.items] == ["docs/a.md", "docs/z.md"]
    assert [i.external_id for i in first.items] == [i.external_id for i in repeat.items]


async def test_s3_fetch_returns_bytes():
    client = _FakeS3Client({"docs/a.md": b"# a\n"})
    conn = S3Connector(injectable_client=client)
    blob = await conn.fetch(_s3_config(), SourceItem(external_id="docs/a.md", name="a.md"))
    assert blob == b"# a\n"
