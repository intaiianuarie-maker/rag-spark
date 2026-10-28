"""Containment regression tests for LocalFolderConnector (M6-001c)."""

from __future__ import annotations

from pathlib import Path

import pytest
from connectors import ConnectorConfig, ConnectorKind
from connectors.adapters.local_connector import LocalFolderConnector

pytestmark = pytest.mark.unit


def _config(root: Path) -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="local",
        workspace_id="ws",
        kind=ConnectorKind.LOCAL_UPLOAD,
        settings={"path": str(root)},
    )


def test_sibling_prefix_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    (root / "a.txt").write_text("a")
    sibling = tmp_path / "approved-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret")
    conn = LocalFolderConnector()
    cfg = _config(root)
    # craft an item whose external_id traverses to sibling
    from connectors.interfaces import SourceItem

    item = SourceItem(external_id="../approved-evil/secret.txt", name="secret.txt")
    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(conn.fetch(cfg, item))


def test_valid_nested_path_allowed(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    nested = root / "sub" / "dir"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("hello")
    conn = LocalFolderConnector()
    cfg = _config(root)
    import asyncio

    from connectors.interfaces import SourceItem

    item = SourceItem(external_id="sub/dir/file.txt", name="file.txt")
    data = asyncio.run(conn.fetch(cfg, item))
    assert data == b"hello"


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    link = root / "link"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")
    conn = LocalFolderConnector()
    cfg = _config(root)
    import asyncio

    from connectors.interfaces import SourceItem

    item = SourceItem(external_id="link/secret.txt", name="secret.txt")
    with pytest.raises(ValueError):
        asyncio.run(conn.fetch(cfg, item))
