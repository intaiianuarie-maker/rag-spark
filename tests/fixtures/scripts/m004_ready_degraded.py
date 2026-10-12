"""`/ready` degraded-contract check — landed from the M0-003 ad-hoc probe (test1).

Provenance: `/root/.tmp/m004_ready_degraded.py` (handoffs/M0-003-backend-test1.md,
`DEGRADED_CHECKS_OK`). Every dependency URL points at a closed port, so `/ready`
must still return HTTP 200 with `status=degraded`, `ready=false` and
per-dependency `reachable=false` + error text, while `/health` and `/version`
stay 200 (liveness is independent of dependencies). Never raises upward; exits
1 with `DEGRADED_CHECKS_FAILED` and the failing checks listed.

T-V1-027: the pinned set is every dependency the enabled modes require —
postgres/redis/qdrant/minio/infinity (stores), `llm_gateway` (model) and
`worker` (queue consumer), plus `graph_store` only when `GRAPH_MODE` is
enabled (off here, exactly as in `tests/integration/test_boot.py` and
`tests/integration/test_live_endpoints.py`). Required-down ⇒ not-ready: all
dead ⇒ `ready=false`, status `degraded`.

Run directly (repo root):

    PYTHONPATH=packages:apps/api python3 tests/fixtures/scripts/m004_ready_degraded.py

Executed as a subprocess by `tests/integration/test_landed_ready_degraded.py`
because it must set DATABASE_URL/REDIS_URL/QDRANT_URL/MINIO_ENDPOINT/LITELLM_URL
*before* the app settings are imported.
"""

from __future__ import annotations

import os
import sys
import time

os.environ["MIGRATIONS_ON_BOOT"] = "false"
os.environ["LOG_JSON"] = "false"
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "postgresql+psycopg://nobody:nothing@127.0.0.1:59999/nope"
os.environ["REDIS_URL"] = "redis://127.0.0.1:59999/0"
os.environ["QDRANT_URL"] = "http://127.0.0.1:59999"
os.environ["MINIO_ENDPOINT"] = "http://127.0.0.1:59999"
os.environ["INFINITY_URL"] = "http://127.0.0.1:59999"
os.environ["LITELLM_URL"] = "http://127.0.0.1:59999"
os.environ["GRAPH_MODE"] = "off"
os.environ["READINESS_TIMEOUT_SECONDS"] = "1.5"
os.environ["READINESS_CAPABILITY_TIMEOUT_SECONDS"] = "1.5"
os.environ["READINESS_FRESHNESS_SECONDS"] = "5.0"

from app.main import create_app
from fastapi.testclient import TestClient

# T-V1-027: identical set in all 3 pinned places (this fixture,
# tests/integration/test_boot.py, tests/integration/test_live_endpoints.py).
BASE_DEPENDENCIES = ("postgres", "redis", "qdrant", "minio", "infinity", "llm_gateway", "worker")
GRAPH_ENABLED = os.environ.get("GRAPH_MODE", "off").strip().lower() not in {"", "off"}
DEPENDENCIES = BASE_DEPENDENCIES + (("graph_store",) if GRAPH_ENABLED else ())

with TestClient(create_app()) as client:
    start = time.perf_counter()
    health = client.get("/health")
    version = client.get("/version")
    ready = client.get("/ready")
    elapsed = time.perf_counter() - start

body = ready.json()
errors: list[str] = []
if health.status_code != 200:
    errors.append(f"/health {health.status_code}")
if version.status_code != 200:
    errors.append(f"/version {version.status_code}")
if ready.status_code != 200:
    errors.append(f"/ready {ready.status_code} (must stay non-fatal)")
if body.get("status") != "degraded":
    errors.append(f"status={body.get('status')!r} expected 'degraded'")
if body.get("ready") is not False:
    errors.append(f"ready={body.get('ready')!r} expected False")
if set(body.get("dependencies", {})) != set(DEPENDENCIES):
    errors.append(
        f"deps={sorted(body.get('dependencies', {}))} expected {sorted(DEPENDENCIES)}"
    )
for name in DEPENDENCIES:
    dependency = body["dependencies"][name]
    if dependency["reachable"] is not False:
        errors.append(f"{name} reachable={dependency['reachable']} expected False")
    if not dependency.get("error"):
        errors.append(f"{name} missing error detail: {dependency}")
    if dependency["name"] != name:
        errors.append(f"{name} name mismatch {dependency['name']}")
if elapsed > 7.5:
    errors.append(
        f"/ready took {elapsed:.2f}s "
        f"(probes run concurrently, bounded by the 5.0s freshness budget)"
    )

print(
    f"/health={health.status_code} /version={version.status_code} "
    f"/ready={ready.status_code} elapsed={elapsed:.2f}s"
)
print(f"ready_body={body}")
if errors:
    print("DEGRADED_CHECKS_FAILED")
    for error in errors:
        print("  -", error)
    sys.exit(1)
print("DEGRADED_CHECKS_OK")
