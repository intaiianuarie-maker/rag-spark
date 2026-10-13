"""Host-side object-store env for tests that write document originals (T-V1-039).

``docker-compose.yml`` hands the API and worker containers ``S3_ENDPOINT_URL``,
``S3_ACCESS_KEY``, ``S3_SECRET_KEY`` and ``S3_BUCKET``; a host ``pytest`` run
gets none of them, and ``storage.originals`` deliberately invents neither an
endpoint nor a credential — an empty endpoint would fall through to AWS
(PD-005 binds originals to the compose SeaweedFS) and a guessed key is a
guessed key.

:func:`apply` fills in the compose defaults **only where nothing is set**, so
an operator with a real object store configured keeps their configuration, then
verifies the endpoint is reachable via the harness's loud-skip helper — a down
store names itself and the command that starts it instead of failing somewhere
deep in an S3 client stack trace.
"""

from __future__ import annotations

import os

from service_probe import require_service

#: Mirrored from ``docker-compose.yml`` (``seaweedfs`` service + ``x-backend-env``).
ENDPOINT = "http://127.0.0.1:8333"
ACCESS_KEY = "admin"
SECRET_KEY = "admin"
BUCKET = "knowledge-os"


def apply() -> str:
    """Make the compose object store usable from this process; return its URL."""
    if not (os.getenv("S3_ENDPOINT_URL") or os.getenv("SEAWEEDFS_ENDPOINT")):
        os.environ["S3_ENDPOINT_URL"] = ENDPOINT
    for s3_name, seaweed_name, value in (
        ("S3_ACCESS_KEY", "SEAWEEDFS_ACCESS_KEY", ACCESS_KEY),
        ("S3_SECRET_KEY", "SEAWEEDFS_SECRET_KEY", SECRET_KEY),
    ):
        if not (os.getenv(s3_name) or os.getenv(seaweed_name)):
            os.environ[s3_name] = value
    os.environ.setdefault("S3_BUCKET", BUCKET)
    endpoint = (
        os.getenv("S3_ENDPOINT_URL")
        or os.getenv("SEAWEEDFS_ENDPOINT")
        or ""
    )
    require_service(
        endpoint,
        "SeaweedFS object store (document originals)",
        "docker compose up -d seaweedfs",
    )
    return endpoint
