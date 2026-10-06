"""Connector contract (`connectors.interfaces.Connector`).

In-memory fake now; M6's local/web/S3/Git connectors run the same
`check_connector_contract` (stable discovery order, cursor paging that never
re-serves an item, honest sync counts — the properties dedup and incremental
sync depend on).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from connectors.contract_tests import check_connector_contract
from connectors.interfaces import ConnectorConfig, ConnectorKind, SourceItem
from harness_fakes import FakeConnector

pytestmark = [pytest.mark.unit, pytest.mark.contract]

CONFIG = ConnectorConfig(
    connector_id="conn-contract",
    workspace_id="ws-a",
    kind=ConnectorKind.LOCAL_UPLOAD,
    settings={"path": "/tmp/does-not-matter"},
)


def _item(external_id: str) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        name=f"{external_id}.md",
        uri=f"file:///{external_id}.md",
        updated_at=datetime.now(UTC),
    )


ITEMS = [_item("doc-1"), _item("doc-2"), _item("doc-3")]


async def test_fake_connector_satisfies_contract() -> None:
    # FakeConnector pages 2 items at a time, so the first page holds two.
    await check_connector_contract(FakeConnector(ITEMS), CONFIG, min_items=2)


async def test_fake_connector_pages_without_re_serving_items() -> None:
    connector = FakeConnector(ITEMS)
    first = await connector.discover(CONFIG)
    second = await connector.discover(CONFIG, cursor=first.next_cursor)
    assert first.next_cursor == "2"
    assert [item.external_id for item in second.items] == ["doc-3"]


async def test_fake_connector_fetch_returns_bytes_and_rejects_unknown_ids() -> None:
    connector = FakeConnector(ITEMS)
    blob = await connector.fetch(CONFIG, ITEMS[0])
    assert isinstance(blob, bytes) and blob
    with pytest.raises(KeyError):
        await connector.fetch(CONFIG, SourceItem(external_id="ghost", name="ghost"))
