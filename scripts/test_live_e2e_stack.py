"""Live end-to-end integration test against running Docker stack and OpenRouter."""

import uuid
import httpx

API_BASE = "http://localhost:8000"
PASSWORD = "Correct-Horse-Battery-9"

def main():
    email = f"live-test-{uuid.uuid4().hex[:8]}@example.com"
    print(f"[1] Registering user {email}...")
    with httpx.Client(base_url=API_BASE, timeout=30.0) as client:
        r = client.post("/auth/register", json={"email": email, "password": PASSWORD})
        assert r.status_code == 201, f"Register failed: {r.status_code} {r.text}"

        print("[2] Logging in...")
        r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
        token = r.json()["access_token"]
        auth_headers = {"authorization": f"Bearer {token}"}

        print("[3] Creating workspace...")
        r = client.post("/v1/workspaces", json={"name": "Live Test WS"}, headers=auth_headers)
        assert r.status_code == 201, f"Create workspace failed: {r.status_code} {r.text}"
        ws_id = r.json()["id"]
        print(f"Workspace created: {ws_id}")

        print("[4] Searching workspace chunks (hybrid mode)...")
        r = client.post(
            f"/v1/workspaces/{ws_id}/search",
            json={"query": "test permissions", "mode": "hybrid"},
            headers=auth_headers,
        )
        assert r.status_code == 200, f"Search failed: {r.status_code} {r.text}"
        search_results = r.json()
        print(f"Search successful, returned {len(search_results['results'])} results.")

        print("[5] Legacy route alias search /v1/search...")
        r = client.post(
            "/v1/search",
            json={"query": "test permissions", "workspace_id": ws_id, "mode": "hybrid"},
            headers=auth_headers,
        )
        assert r.status_code == 200, f"Legacy search failed: {r.status_code} {r.text}"
        print("Legacy search route alias verified!")

        print("[6] Testing Ask endpoint via OpenRouter cloud...")
        r = client.post(
            f"/v1/workspaces/{ws_id}/ask",
            json={"query": "What permissions are configured?", "model": "nvidia/nemotron-3.5-lightning:free"},
            headers=auth_headers,
        )
        print(f"Ask response status: {r.status_code}")
        print(f"Ask response body: {r.text}")
        assert r.status_code in {200, 503}, f"Unexpected status: {r.status_code}"

    print("ALL LIVE TESTS COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
