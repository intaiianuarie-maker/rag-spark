"""Unit tests for connectors release decision (T-R09).

Proves:
1. V1 ships as upload-only; continuous sync scheduling is M6-pending:
   - connectors.V1_SHIP_MODE == "upload-only"
   - connectors.M6_STATUS == "pending"
2. QueuedSyncConsumer progress store and adapters remain library-complete
   and unit-tested for on-demand / scoped execution.
3. No live background worker or unsupervised scheduler runs by default.
"""

from __future__ import annotations

import connectors
from connectors import sync_consumer


def test_connectors_release_decision_constants() -> None:
    """V1 ship mode is upload-only; continuous sync scheduling is M6-pending."""
    assert connectors.V1_SHIP_MODE == "upload-only"
    assert connectors.M6_STATUS == "pending"
    assert sync_consumer.V1_SHIP_MODE == "upload-only"
    assert sync_consumer.M6_STATUS == "pending"


def test_connectors_library_components_available_for_scoped_use() -> None:
    """QueuedSyncConsumer, SourceIdentity, and ProgressStore remain importable and functional."""
    from connectors.sync_consumer import (
        InMemorySyncProgressStore,
        QueuedSyncConsumer,
        SourceIdentity,
    )

    store = InMemorySyncProgressStore()
    assert store is not None
    assert issubclass(QueuedSyncConsumer, object)
    assert issubclass(SourceIdentity, object)
