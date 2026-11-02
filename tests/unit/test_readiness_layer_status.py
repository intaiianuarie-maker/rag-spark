"""effective_layer_status: vision and multimodal follow env flag + key, never hard-coded."""

from types import SimpleNamespace

import pytest
from app.adapters.readiness import effective_layer_status
from core.health import DependencyStatus

KEYS = ("OPENROUTER_API_KEY", "LITELLM_API_KEY", "DEEPSEEK_API_KEY")
FLAGS = ("VISION_ENABLED", "MULTIMODAL_EMBED_ENABLED")
LAYERS = [("vision", "VISION_ENABLED"), ("multimodal", "MULTIMODAL_EMBED_ENABLED")]
CONFIGURED_REASON = "configured (flag on + OPENROUTER_API_KEY); no product run recorded"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in KEYS + FLAGS:
        monkeypatch.delenv(name, raising=False)


def _dep(name, ok=True):
    return DependencyStatus(name=name, reachable=ok)


def _status(graph_mode="off", deps=None):
    return effective_layer_status(SimpleNamespace(graph_mode=graph_mode), deps or {})


@pytest.mark.parametrize("layer,flag", LAYERS)
def test_flag_off_by_default(layer, flag):
    entry = _status()[layer]
    assert entry["configured"] is False
    assert entry["enabled"] is False
    assert flag in entry["reason"]


@pytest.mark.parametrize("layer,flag", LAYERS)
def test_flag_on_without_key(monkeypatch, layer, flag):
    monkeypatch.setenv(flag, "true")
    entry = _status()[layer]
    assert entry["configured"] is False
    assert entry["enabled"] is False
    assert "OPENROUTER_API_KEY" in entry["reason"]


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
@pytest.mark.parametrize("layer,flag", LAYERS)
def test_flag_on_with_key(monkeypatch, layer, flag, value):
    monkeypatch.setenv(flag, value)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    entry = _status()[layer]
    assert entry["configured"] is True
    assert entry["enabled"] is True
    assert entry["reason"] == CONFIGURED_REASON


# The worker's vision/multimodal steps read OPENROUTER_API_KEY only and skip
# otherwise, so other provider keys must not count as configured here.
@pytest.mark.parametrize("key", ["LITELLM_API_KEY", "DEEPSEEK_API_KEY"])
@pytest.mark.parametrize("layer,flag", LAYERS)
def test_flag_on_with_other_provider_key_only(monkeypatch, layer, flag, key):
    monkeypatch.setenv(flag, "true")
    monkeypatch.setenv(key, "k")
    entry = _status()[layer]
    assert entry["configured"] is False
    assert entry["enabled"] is False
    assert "OPENROUTER_API_KEY" in entry["reason"]


@pytest.mark.parametrize("value", ["", "   ", "\t"])
@pytest.mark.parametrize("layer,flag", LAYERS)
def test_flag_on_with_blank_openrouter_key(monkeypatch, layer, flag, value):
    monkeypatch.setenv(flag, "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", value)
    entry = _status()[layer]
    assert entry["configured"] is False
    assert entry["enabled"] is False


def test_flag_falsey_value_with_key(monkeypatch):
    monkeypatch.setenv("VISION_ENABLED", "no")
    monkeypatch.setenv("LITELLM_API_KEY", "k")
    assert _status()["vision"]["enabled"] is False


def test_graph_mode_off():
    entry = _status("off")["graph"]
    assert entry["enabled"] is False
    assert "off" in entry["reason"]


def test_graph_store_unreachable():
    entry = _status("llm", {"graph_store": _dep("graph_store", ok=False)})["graph"]
    assert entry["enabled"] is False
    assert "unreachable" in entry["reason"]


def test_graph_on_and_store_ok():
    assert _status("llm", {"graph_store": _dep("graph_store")})["graph"]["enabled"] is True


# GRAPH_MODE=rule is the rule graph, not the LLM graph layer (plan section 12).
@pytest.mark.parametrize("store_ok", [True, False])
def test_graph_rule_mode_is_not_the_llm_layer(store_ok):
    entry = _status("rule", {"graph_store": _dep("graph_store", ok=store_ok)})["graph"]
    assert entry["enabled"] is False
    assert entry["reason"] == "rule mode is not the LLM graph layer"


def test_missing_deps_disable_agents_and_research(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    status = _status()
    assert status["agents"]["enabled"] is False
    assert status["research"]["enabled"] is False
    assert status["workflows"]["enabled"] is False


def test_agents_enabled_with_gateway_and_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    status = _status(deps={"llm_gateway": _dep("llm_gateway")})
    assert status["agents"]["enabled"] is True
    assert status["research"]["enabled"] is True
