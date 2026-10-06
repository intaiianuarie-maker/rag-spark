"""Contract checks for the connector interface (`connectors.interfaces.Connector`).

Test-only (TASK-M0-004). Pass any `Connector` (the M0 harness passes an
in-memory fake; M6 passes local/web/S3/Git connectors). What is locked here is
the incremental-sync shape M2/M6 dedup depends on: stable discovery order,
cursor paging that never re-serves an item, and a `ConnectorSyncResult` whose
counts and errors are trustworthy.
"""

from __future__ import annotations

from collections.abc import Awaitable

from .interfaces import (
    Connector,
    ConnectorConfig,
    ConnectorDiscovery,
    ConnectorKind,
    ConnectorSyncResult,
    SourceItem,
)


def _fail(message: str) -> None:
    raise AssertionError(message)


def _require_instance(value: object, expected_type: type, what: str) -> None:
    if not isinstance(value, expected_type):
        _fail(f"{what}: expected {expected_type.__name__}, got {type(value).__name__}")


async def _expect_error(awaitable: Awaitable[object], what: str) -> None:
    """Invalid input must raise loudly (no phantom bytes for a missing item)."""
    try:
        result = await awaitable
    except Exception:  # any loud failure satisfies the contract
        return
    _fail(f"{what}: expected an exception, but returned {result!r}")


async def check_connector_contract(
    connector: Connector,
    config: ConnectorConfig,
    *,
    min_items: int = 1,
) -> None:
    """discover / fetch / sync shapes, including cursor and idempotency guards."""
    _require_instance(connector.kind, ConnectorKind, "Connector.kind")

    first = await connector.discover(config)
    _require_instance(first, ConnectorDiscovery, "discover return value")
    if len(first.items) < min_items:
        _fail(f"discover returned {len(first.items)} items, expected at least {min_items}")
    for item in first.items:
        _require_instance(item, SourceItem, "discovered item")
        if not item.external_id or not item.name:
            _fail(f"discovered item needs external_id and name: {item}")

    repeat = await connector.discover(config)
    if [item.external_id for item in repeat.items] != [
        item.external_id for item in first.items
    ]:
        _fail("discover order is not stable across calls (breaks incremental sync)")

    blob = await connector.fetch(config, first.items[0])
    _require_instance(blob, bytes, "fetch return value")

    if first.next_cursor is not None:
        second_page = await connector.discover(config, cursor=first.next_cursor)
        overlap = {item.external_id for item in first.items} & {
            item.external_id for item in second_page.items
        }
        if overlap:
            _fail(f"cursor paging re-served items (duplicate-sync risk): {sorted(overlap)}")

    sync_result = await connector.sync(config)
    _require_instance(sync_result, ConnectorSyncResult, "sync return value")
    if sync_result.fetched > sync_result.discovered:
        _fail(
            f"sync reported fetched={sync_result.fetched} > discovered={sync_result.discovered}"
        )

    await _expect_error(
        connector.fetch(config, SourceItem(external_id="missing-item", name="missing")),
        "fetch of an item that does not exist",
    )
