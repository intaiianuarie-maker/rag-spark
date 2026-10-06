"""`/ready` degraded-contract check — landed from the M0-003 ad-hoc probe (test1).

Provenance: `/root/.tmp/m004_ready_degraded.py` (handoffs/M0-003-backend-test1.md,
`DEGRADED_CHECKS_OK`). Every dependency URL points at a closed port, so `/ready`
must still return HTTP 200 with `status=degraded`, `ready=false` and
per-dependency `reachable=false` + error text, while `/health` and `/version`
stay 200 (liveness is independent of dependencies). Never raises upward; exits
1 with `DEGRADED_CHECKS_FAILED` and the failing checks listed.

Run directly (repo root):

    PYTHONPATH=packages:apps/api python3 tests/fixtures/scripts/m004_ready_degraded.py

Executed as a subprocess by `tests/integration/test_landed_ready_degraded.py`
because it must set DATABASE_URL/REDIS_URL/QDRANT_URL/MINIO_ENDPOINT *before*
the app settings are imported.
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
os.environ["READINESS_TIMEOUT_SECONDS"] = "1.5"

from app.main import create_app
from fastapi.testclient import TestClient

DEPS = ("postgres", "redis", "qdrant", "minio")

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
if set(body.get("dependencies", {})) != set(DEPS):
    errors.append(f"deps={sorted(body.get('dependencies', {}))} expected {sorted(DEPS)}")
for name in DEPS:
    dependency = body["dependencies"][name]
    if dependency["reachable"] is not False:
        errors.append(f"{name} reachable={dependency['reachable']} expected False")
    if not dependency.get("error"):
        errors.append(f"{name} missing error detail: {dependency}")
    if dependency["name"] != name:
        errors.append(f"{name} name mismatch {dependency['name']}")
if elapsed > 6.0:
    errors.append(
        f"/ready took {elapsed:.2f}s (4 probes x 1.5s timeout should be concurrent)"
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
