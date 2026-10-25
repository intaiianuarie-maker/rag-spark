"""Unit tests for M7-001a provider registry (no network)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")

from models.providers.openai_compat import OpenAICompatProvider
from models.registry import (
    Capability,
    ModelRegistry,
    UnknownModelError,
    capability_query,
    register,
    resolve,
)

pytestmark = pytest.mark.unit


def test_register_resolve_capability_unknown() -> None:
    reg = ModelRegistry()
    p = OpenAICompatProvider(
        name="local-stub",
        base_url="http://127.0.0.1:9999/v1",
        model="stub-model",
        capabilities={"chat", "multilingual"},
    )
    reg.register("stub-model", p)
    assert reg.resolve("stub-model") is p
    assert "stub-model" in reg.capability_query("chat")
    assert "stub-model" in reg.capability_query("multilingual")
    assert reg.capability_query("reasoning") == []
    with pytest.raises(UnknownModelError):
        reg.resolve("nope")


def test_capability_object() -> None:
    reg = ModelRegistry()
    p = OpenAICompatProvider(
        name="cloud",
        base_url="http://example.test/v1",
        model="cloud-model",
        capabilities=Capability(chat=True, reasoning=True, coding=False),
    )
    reg.register("cloud-model", p)
    assert reg.supports("cloud-model", "chat")
    assert reg.supports("cloud-model", "reasoning")
    assert not reg.supports("cloud-model", "coding")


def test_module_level_helpers() -> None:
    register("helper-model", base_url="http://x/v1", capabilities=["chat"])
    assert resolve("helper-model") is not None
    assert "helper-model" in capability_query("chat")
