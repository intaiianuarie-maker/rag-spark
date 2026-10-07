"""M1-001b regression pinned by code2's review — invited-user list exclusion.

"Suggested Additional Tests": `GET /v1/workspaces` must not list a workspace to
an *invited* (not yet accepted) user — the workspace-switcher source for
M1-002b. Previously live-checked only; asserted here so a later membership or
join change cannot silently expose invitations as if they were active
memberships.

Uses the harness `api_client` fixture: skips loudly when compose is down.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration

PASSWORD = "S3cure-passw0rd!x"


def _identity(api_client, label: str) -> tuple[str, str]:
    email = f"m1001b-reg-{label}-{uuid.uuid4().hex[:10]}@example.invalid"
    register = api_client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert register.status_code == 201, register.text
    login = api_client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return email, login.json()["access_token"]


def test_invited_user_is_excluded_from_the_switcher_until_accept(api_client) -> None:
    _owner_email, owner_token = _identity(api_client, "owner")
    invitee_email, invitee_token = _identity(api_client, "invitee")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    invitee_headers = {"Authorization": f"Bearer {invitee_token}"}

    created = api_client.post(
        "/v1/workspaces", json={"name": f"reg-{uuid.uuid4().hex[:8]}"}, headers=owner_headers
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]

    invited = api_client.post(
        f"/v1/workspaces/{workspace_id}/members",
        json={"email": invitee_email, "role": "editor"},
        headers=owner_headers,
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["status"] == "invited", invited.text

    # --- the regression: invited memberships are NOT active memberships ---
    invitee_list = api_client.get("/v1/workspaces", headers=invitee_headers)
    assert invitee_list.status_code == 200, invitee_list.text
    listed_ids = [workspace["id"] for workspace in invitee_list.json()]
    assert workspace_id not in listed_ids, (
        "invited (pre-accept) user sees the workspace in GET /v1/workspaces — "
        f"switcher would show an inaccessible workspace: {listed_ids}"
    )

    before_accept = api_client.get(f"/v1/workspaces/{workspace_id}", headers=invitee_headers)
    assert before_accept.status_code == 403, (before_accept.status_code, before_accept.text)

    accepted = api_client.post(
        f"/v1/workspaces/{workspace_id}/members/accept", headers=invitee_headers
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "active", accepted.text

    after_list = api_client.get("/v1/workspaces", headers=invitee_headers)
    assert after_list.status_code == 200, after_list.text
    assert workspace_id in [workspace["id"] for workspace in after_list.json()], (
        "active membership missing from GET /v1/workspaces after accept"
    )

    after_get = api_client.get(f"/v1/workspaces/{workspace_id}", headers=invitee_headers)
    assert after_get.status_code == 200, (after_get.status_code, after_get.text)
    assert after_get.json()["role"] == "editor", after_get.text


def test_role_changes_apply_to_the_switcher_view(api_client) -> None:
    """Role demotion is visible through the list contract (M1-002b badge source)."""
    _, owner_token = _identity(api_client, "owner2")
    member_email, member_token = _identity(api_client, "member2")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}

    created = api_client.post(
        "/v1/workspaces", json={"name": f"reg-{uuid.uuid4().hex[:8]}"}, headers=owner_headers
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]

    invited = api_client.post(
        f"/v1/workspaces/{workspace_id}/members",
        json={"email": member_email, "role": "editor"},
        headers=owner_headers,
    )
    assert invited.status_code == 201, invited.text
    assert (
        api_client.post(
            f"/v1/workspaces/{workspace_id}/members/accept", headers=member_headers
        ).status_code
        == 200
    )

    demoted = api_client.patch(
        f"/v1/workspaces/{workspace_id}/members/{invited.json()['user_id']}",
        json={"role": "viewer"},
        headers=owner_headers,
    )
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["role"] == "viewer", demoted.text

    listed = api_client.get("/v1/workspaces", headers=member_headers)
    assert listed.status_code == 200, listed.text
    roles = {w["id"]: w["role"] for w in listed.json()}
    assert roles.get(workspace_id) == "viewer", (
        f"switcher role badge source stale after demotion: {roles}"
    )
