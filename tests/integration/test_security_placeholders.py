"""Security E2E placeholders — xfail(strict=False) until the owning tasks land.

TASK-M0-004 requirement 4: workspace-isolation, deleted-document and
duplicate-sync checks must exist now and be turned green *without rewriting the
harness*. Each test asserts the real enforced behaviour, fails today only
because the endpoints do not exist yet, and is marked `xfail(strict=False)` so
M1/M2/M3 implementations flip it to XPASS and then to a normal pass by removing
the marker in their own task.

`strict=False` is deliberate: a passing implementation must never break the
suite, and a still-missing implementation must never look like a green test.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _client():
    from app.main import create_app
    from fastapi.testclient import TestClient

    return TestClient(create_app())


@pytest.mark.xfail(
    strict=False,
    reason="R-004 cross-workspace isolation: needs M1-001c can() + M3-001b /v1/search "
    "(workspace A secret phrase must return zero results for workspace B)",
)
def test_workspace_b_cannot_search_workspace_a_secret(workspaces) -> None:
    """Critical Security E2E from docs/engineering/testing-strategy.md."""
    with _client() as client:
        response = client.post(
            "/v1/search",
            json={"query": workspaces.a.secret_phrase, "workspace_id": workspaces.b.id},
            headers={"x-user-id": workspaces.b.principal.user_id},
        )
    assert response.status_code == 200, (
        f"search endpoint not implemented yet: {response.status_code}"
    )
    results = response.json().get("results", [])
    assert results == [], (
        f"R-004 cross-workspace leak: {workspaces.b.id} saw "
        f"{len(results)} result(s) for another workspace's secret"
    )


@pytest.mark.xfail(
    strict=False,
    reason="tombstones: M2-001d dedup/versions/tombstones (PD-005 first) — deleted "
    "documents must be excluded from retrieval and answerable as tombstones",
)
def test_deleted_document_is_tombstoned_and_never_retrieved(workspaces) -> None:
    deleted_id = "doc-deleted-fixture"
    with _client() as client:
        detail = client.get(f"/v1/documents/{deleted_id}")
        search = client.post(
            "/v1/search",
            json={"query": "deleted document evidence", "workspace_id": workspaces.a.id},
            headers={"x-user-id": workspaces.a.principal.user_id},
        )
    assert detail.status_code in {200, 410, 404}, detail.status_code
    assert detail.status_code != 404, f"document endpoint not implemented yet: {detail.status_code}"
    if detail.status_code == 200:
        body = detail.json()
        assert body.get("deleted") is True or body.get("status") == "deleted", (
            f"deleted document served as live: {body}"
        )
    assert search.status_code == 200, search.status_code
    returned_ids = [item.get("document_id") for item in search.json().get("results", [])]
    assert deleted_id not in returned_ids, (
        f"deleted document still retrievable: {returned_ids}"
    )


@pytest.mark.xfail(
    strict=False,
    reason="duplicate sync: M2-001d dedup + M6-001b incremental sync — syncing twice "
    "must not create duplicate documents",
)
def test_duplicate_sync_does_not_duplicate_documents(workspaces) -> None:
    connector_id = "conn-local-fixture"
    with _client() as client:
        first = client.post(
            f"/v1/connectors/{connector_id}/sync",
            json={"workspace_id": workspaces.a.id},
            headers={"x-user-id": workspaces.a.principal.user_id},
        )
        second = client.post(
            f"/v1/connectors/{connector_id}/sync",
            json={"workspace_id": workspaces.a.id},
            headers={"x-user-id": workspaces.a.principal.user_id},
        )
        listing = client.get(
            "/v1/documents", params={"workspace_id": workspaces.a.id}
        )
    assert first.status_code == 200, f"connector sync not implemented yet: {first.status_code}"
    assert second.status_code == 200, second.status_code
    assert listing.status_code == 200, listing.status_code
    document_ids = [item["id"] for item in listing.json().get("items", [])]
    assert len(document_ids) == len(set(document_ids)), (
        f"duplicate documents after re-sync: {sorted(document_ids)}"
    )
