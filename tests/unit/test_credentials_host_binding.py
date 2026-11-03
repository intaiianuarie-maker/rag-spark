"""Host-bound credential selection: a key only ever goes to its own host.

Plain sync pytest; fake key strings only.
"""

from __future__ import annotations

import pytest
from models.adapters.openrouter_provider import OpenRouterProvider
from models.credentials import credential_for_url, is_openrouter_url, resolve_gateway

pytestmark = pytest.mark.unit

OR = "sk-or-fake"
LL = "sk-litellm-fake"
BOTH = {"OPENROUTER_API_KEY": OR, "LITELLM_API_KEY": LL}

LOOKALIKES = [
    "https://openrouter.ai.evil.example/v1",
    "https://evil.example/openrouter.ai",
    "http://openrouter.ai@evil.example/",
    "https://openrouter.ai@evil.example/",
    "https://openrouter.ai:443@evil.example/v1",
    "https://evil.example\\@openrouter.ai/v1",
    "https://notopenrouter.ai/v1",
    "https://evil-openrouter.ai/v1",
    "https://openrouter.ai:bad/v1",
    "https://openrouter.ai./v1",
    "",
]


@pytest.mark.parametrize("url", LOOKALIKES)
def test_lookalike_never_gets_the_openrouter_key(url) -> None:
    assert not is_openrouter_url(url)
    assert credential_for_url(url, BOTH) == LL
    assert credential_for_url(url, {"OPENROUTER_API_KEY": OR}) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1/",
        "https://OpenRouter.AI/api/v1",
        "https://openrouter.ai:443/api/v1",
        "https://eu.openrouter.ai/api/v1",
    ],
)
def test_openrouter_host_gets_only_the_openrouter_key(url) -> None:
    assert is_openrouter_url(url)
    assert credential_for_url(url, BOTH) == OR
    # LITELLM key alone is never sent to OpenRouter.
    assert credential_for_url(url, {"LITELLM_API_KEY": LL}) is None


@pytest.mark.parametrize(
    "url", ["https://gateway.example/v1", "http://localhost:4000", "http://litellm:4000/"]
)
def test_other_host_gets_only_the_litellm_key(url) -> None:
    assert credential_for_url(url, BOTH) == LL
    assert credential_for_url(url, {"OPENROUTER_API_KEY": OR}) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://openrouter.ai/api/v1",
        "http://eu.openrouter.ai/api/v1",
        "ftp://openrouter.ai/api/v1",
    ],
)
def test_cleartext_openrouter_host_is_refused_not_given_litellm_key(url) -> None:
    assert not is_openrouter_url(url)
    assert credential_for_url(url, BOTH) is None
    assert credential_for_url(url, {"LITELLM_API_KEY": LL}) is None
    assert credential_for_url(url, {"OPENROUTER_API_KEY": OR}) is None


def test_deepseek_host_prefers_deepseek_key_and_falls_back_to_litellm() -> None:
    env = {**BOTH, "DEEPSEEK_API_KEY": "sk-ds-fake"}
    assert credential_for_url("https://api.deepseek.com/v1", env) == "sk-ds-fake"
    assert credential_for_url("https://deepseek.com.evil.example/v1", env) == LL
    # Regression: LITELLM_URL=https://api.deepseek.com with only LITELLM_API_KEY.
    assert credential_for_url("https://api.deepseek.com", {"LITELLM_API_KEY": LL}) == LL
    # The OpenRouter key never goes to DeepSeek.
    assert credential_for_url("https://api.deepseek.com", {"OPENROUTER_API_KEY": OR}) is None


def test_neither_key_set_is_none() -> None:
    assert credential_for_url("https://openrouter.ai/api/v1", {}) is None
    assert credential_for_url("https://gateway.example/v1", {"LITELLM_API_KEY": "  "}) is None


def test_default_config_resolves_as_before() -> None:
    env = {"OPENROUTER_API_KEY": OR, "LITELLM_URL": "https://openrouter.ai/api/v1"}
    assert resolve_gateway(env) == ("https://openrouter.ai/api/v1", OR)
    # Both keys set: still the OpenRouter key.
    assert resolve_gateway({**env, "LITELLM_API_KEY": LL}) == ("https://openrouter.ai/api/v1", OR)


def test_litellm_only_config() -> None:
    env = {"LITELLM_URL": "https://gateway.example/v1/", "LITELLM_API_KEY": LL}
    assert resolve_gateway(env) == ("https://gateway.example/v1", LL)
    assert resolve_gateway({"LITELLM_URL": "https://gateway.example/v1"}) == (
        "https://gateway.example/v1",
        None,
    )


def test_explicit_key_is_the_callers_choice() -> None:
    assert resolve_gateway(BOTH, base_url="https://gateway.example/v1", api_key="mine") == (
        "https://gateway.example/v1",
        "mine",
    )


def _clear(monkeypatch) -> None:
    for k in ("OPENROUTER_API_KEY", "LITELLM_API_KEY", "LITELLM_URL", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_provider_both_keys_openrouter_host_sends_openrouter_key(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", OR)
    monkeypatch.setenv("LITELLM_API_KEY", LL)
    p = OpenRouterProvider()
    assert p.base_url == "https://openrouter.ai/api/v1"
    assert p.api_key == OR
    assert p._headers()["authorization"] == f"Bearer {OR}"
    assert p._headers()["HTTP-Referer"]
    assert p.transport.api_key == OR


@pytest.mark.parametrize("url", LOOKALIKES[:6])
def test_provider_lookalike_url_never_carries_openrouter_key(monkeypatch, url) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", OR)
    p = OpenRouterProvider(base_url=url)
    assert p.api_key is None
    assert OR not in p._headers().get("authorization", "")
    assert "HTTP-Referer" not in p._headers()
    # The transport must not re-pick the env OpenRouter key either.
    assert p.transport.api_key in ("", None)
    assert "authorization" not in p.transport.build_headers()


def test_provider_litellm_host_gets_litellm_key_only(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", OR)
    monkeypatch.setenv("LITELLM_API_KEY", LL)
    p = OpenRouterProvider(base_url="https://gateway.example/v1")
    assert p.api_key == LL
    assert p.transport.api_key == LL


def test_provider_no_key_means_no_authorization(monkeypatch) -> None:
    _clear(monkeypatch)
    p = OpenRouterProvider(base_url="https://gateway.example/v1")
    assert p.api_key is None
    assert "authorization" not in p._headers()


def test_agent_gate_uses_host_bound_key(monkeypatch) -> None:
    from agents.factories import resolve_agent_llm_provider

    _clear(monkeypatch)
    monkeypatch.setenv("LITELLM_URL", "https://gateway.example/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", OR)
    # OPENROUTER key set -> provider talks to OpenRouter with that key.
    p = resolve_agent_llm_provider()
    assert p is not None and p.api_key == OR and p.base_url == "https://openrouter.ai/api/v1"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert resolve_agent_llm_provider() is None  # custom host but no LITELLM key
    monkeypatch.setenv("LITELLM_API_KEY", LL)
    assert resolve_agent_llm_provider().api_key == LL


def test_graph_llm_mode_refuses_without_the_hosts_key(monkeypatch) -> None:
    from graph.dispatch import GraphModeUnsupported, build_llm_provider

    _clear(monkeypatch)
    monkeypatch.setenv("LITELLM_URL", "https://gateway.example/v1")
    with pytest.raises(GraphModeUnsupported):
        build_llm_provider()
    monkeypatch.setenv("LITELLM_API_KEY", LL)
    assert build_llm_provider().api_key == LL  # type: ignore[attr-defined]


def test_rerank_refuses_a_non_openrouter_url_with_the_openrouter_key(monkeypatch) -> None:
    from rerank.adapters.openrouter_provider import (
        NemotronRerankProvider,
        RerankAvailabilityError,
    )

    monkeypatch.setenv("OPENROUTER_API_KEY", OR)
    with pytest.raises(RerankAvailabilityError):
        NemotronRerankProvider(base_url="https://openrouter.ai.evil.example/api/v1/rerank")
    assert NemotronRerankProvider().api_key == OR
