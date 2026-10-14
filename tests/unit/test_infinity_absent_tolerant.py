"""Unit tests: deleted infinity is absent-tolerant for /ready (T-F1-READY)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from app.adapters import readiness
from core.config import Settings
from core.health import DependencyStatus

pytestmark = pytest.mark.unit


def _healthy(name: str) -> DependencyStatus:
    return DependencyStatus(name=name, reachable=True, latency_ms=1.0, detail="ok")


def _down(name: str) -> DependencyStatus:
    return DependencyStatus(name=name, reachable=False, latency_ms=1.0, error="down")


@pytest.mark.asyncio
async def test_infinity_unset_ready_ignores_it(monkeypatch) -> None:
    settings = Settings(_env_file=None, infinity_url="")
    monkeypatch.setattr(readiness, "get_settings", lambda: settings)
    monkeypatch.setattr(readiness.postgres, "probe", AsyncMock(return_value=_healthy("postgres")))
    monkeypatch.setattr(
        readiness.redis_probe, "probe", AsyncMock(return_value=_healthy("redis"))
    )
    monkeypatch.setattr(
        readiness.qdrant_probe, "probe", AsyncMock(return_value=_healthy("qdrant"))
    )
    monkeypatch.setattr(
        readiness.minio_probe, "probe", AsyncMock(return_value=_healthy("minio"))
    )
    # Still go through the probe so the absent-tolerant branch is exercised.
    infinity_result = await readiness.infinity_probe.probe("", 0.1)
    monkeypatch.setattr(
        readiness.infinity_probe,
        "probe",
        AsyncMock(return_value=infinity_result),
    )
    monkeypatch.setattr(
        readiness.model_gateway,
        "probe",
        AsyncMock(return_value=_healthy("llm_gateway")),
    )
    monkeypatch.setattr(
        readiness.worker_liveness, "probe", AsyncMock(return_value=_healthy("worker"))
    )

    deps = await readiness.check_dependencies()

    assert deps["infinity"].reachable is True
    assert all(dep.reachable for dep in deps.values())


@pytest.mark.asyncio
async def test_infinity_set_unreachable_degraded(monkeypatch) -> None:
    settings = Settings(_env_file=None, infinity_url="http://127.0.0.1:9")
    monkeypatch.setattr(readiness, "get_settings", lambda: settings)
    monkeypatch.setattr(readiness.postgres, "probe", AsyncMock(return_value=_healthy("postgres")))
    monkeypatch.setattr(
        readiness.redis_probe, "probe", AsyncMock(return_value=_healthy("redis"))
    )
    monkeypatch.setattr(
        readiness.qdrant_probe, "probe", AsyncMock(return_value=_healthy("qdrant"))
    )
    monkeypatch.setattr(
        readiness.minio_probe, "probe", AsyncMock(return_value=_healthy("minio"))
    )
    monkeypatch.setattr(
        readiness.infinity_probe,
        "probe",
        AsyncMock(return_value=_down("infinity")),
    )
    monkeypatch.setattr(
        readiness.model_gateway,
        "probe",
        AsyncMock(return_value=_healthy("llm_gateway")),
    )
    monkeypatch.setattr(
        readiness.worker_liveness, "probe", AsyncMock(return_value=_healthy("worker"))
    )

    deps = await readiness.check_dependencies()

    assert deps["infinity"].reachable is False
    assert not all(dep.reachable for dep in deps.values())


@pytest.mark.asyncio
async def test_pg_down_not_ready(monkeypatch) -> None:
    settings = Settings(_env_file=None, infinity_url="")
    monkeypatch.setattr(readiness, "get_settings", lambda: settings)
    monkeypatch.setattr(readiness.postgres, "probe", AsyncMock(return_value=_down("postgres")))
    monkeypatch.setattr(
        readiness.redis_probe, "probe", AsyncMock(return_value=_healthy("redis"))
    )
    monkeypatch.setattr(
        readiness.qdrant_probe, "probe", AsyncMock(return_value=_healthy("qdrant"))
    )
    monkeypatch.setattr(
        readiness.minio_probe, "probe", AsyncMock(return_value=_healthy("minio"))
    )
    monkeypatch.setattr(
        readiness.infinity_probe,
        "probe",
        AsyncMock(return_value=_healthy("infinity")),
    )
    monkeypatch.setattr(
        readiness.model_gateway,
        "probe",
        AsyncMock(return_value=_healthy("llm_gateway")),
    )
    monkeypatch.setattr(
        readiness.worker_liveness, "probe", AsyncMock(return_value=_healthy("worker"))
    )

    deps = await readiness.check_dependencies()

    assert deps["postgres"].reachable is False
    assert not all(dep.reachable for dep in deps.values())


@pytest.mark.asyncio
async def test_qdrant_down_not_ready(monkeypatch) -> None:
    settings = Settings(_env_file=None, infinity_url="")
    monkeypatch.setattr(readiness, "get_settings", lambda: settings)
    monkeypatch.setattr(readiness.postgres, "probe", AsyncMock(return_value=_healthy("postgres")))
    monkeypatch.setattr(
        readiness.redis_probe, "probe", AsyncMock(return_value=_healthy("redis"))
    )
    monkeypatch.setattr(
        readiness.qdrant_probe, "probe", AsyncMock(return_value=_down("qdrant"))
    )
    monkeypatch.setattr(
        readiness.minio_probe, "probe", AsyncMock(return_value=_healthy("minio"))
    )
    monkeypatch.setattr(
        readiness.infinity_probe,
        "probe",
        AsyncMock(return_value=_healthy("infinity")),
    )
    monkeypatch.setattr(
        readiness.model_gateway,
        "probe",
        AsyncMock(return_value=_healthy("llm_gateway")),
    )
    monkeypatch.setattr(
        readiness.worker_liveness, "probe", AsyncMock(return_value=_healthy("worker"))
    )

    deps = await readiness.check_dependencies()

    assert deps["qdrant"].reachable is False
    assert not all(dep.reachable for dep in deps.values())
