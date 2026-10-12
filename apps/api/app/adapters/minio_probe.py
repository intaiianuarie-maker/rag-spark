"""MinIO / S3 readiness probe.

Hits the MinIO liveness endpoint over HTTP via httpx (no ``minio``/``boto3``
SDK in M0). The S3 storage adapter lands later behind the ingestion boundary.
"""

from __future__ import annotations

import time

import httpx
from core.health import DependencyStatus


async def probe(
    minio_endpoint: str, timeout_seconds: float, secure: bool = False
) -> DependencyStatus:
    name = "minio"
    start = time.perf_counter()
    endpoint = minio_endpoint.rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        scheme = "https" if secure else "http"
        endpoint = f"{scheme}://{endpoint}"
    url = endpoint + "/minio/health/live"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            try:
                response = await client.get(url)
            except httpx.TimeoutException:
                raise
            except Exception:
                response = None
            if response is None or response.status_code != 200:
                response = await client.get(endpoint + "/")
        latency = (time.perf_counter() - start) * 1000
        reachable = response.status_code in {200, 403, 405}
        return DependencyStatus(
            name=name,
            reachable=reachable,
            latency_ms=round(latency, 2),
            detail=f"GET {response.url.path} -> {response.status_code}",
        )
    except Exception as exc:
        latency = (time.perf_counter() - start) * 1000
        return DependencyStatus(
            name=name,
            reachable=False,
            latency_ms=round(latency, 2),
            error=f"{type(exc).__name__}: {exc}",
        )
