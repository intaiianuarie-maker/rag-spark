"""T-V1-029 — local-only means local or explicit unavailable.

Pins the ticket's two instructions end-to-end, with DUMMY transports only (no
real network anywhere — every HTTP client is an in-memory recorder):

1. ``local-only`` resolves to an EXPLICITLY allowed local provider — an
   operator-declared local model over the explicitly local transport — or
   fails closed with the documented unavailable error. The sentinel label is
   never returned as a routable model.
2. The sensitive-fixture tests (case A: fail-closed 503; case B: local
   transport used) drive the REAL Ask handler: a local-only request either
   uses the local transport or returns the explicit 503; in BOTH cases the
   external gateway is never invoked (the ticket's metric: external
   invocations under local-only: 0).

Metrics pinned here: external invocations under local-only: 0 (every case
asserts the recorder saw nothing); explicit-unavailable cases: covered
(no local model configured, and a local endpoint that is not local).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
for _p in (REPO / "packages", REPO / "apps" / "api"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.unit

from answer.models import Answer  # noqa: E402
from app.ask.router import AskRequest, ask, load_model_config  # noqa: E402
from core.auth import Principal, Role  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from models.adapters.litellm_provider import (  # noqa: E402
    LiteLLMProvider,
    LLMProviderError,
)
from models.interfaces import ChatMessage, GenerationRequest, MessageRole  # noqa: E402
from models.router_config import (  # noqa: E402
    DEFAULT_LOCAL_GATEWAY_URL,
    LOCAL_GATEWAY_EXTRA_HOSTS_ENV,
    LOCAL_GATEWAY_URL_ENV,
    LOCAL_ONLY_MODEL_ENV,
    LOCAL_ONLY_SENTINEL,
    MODEL_REGISTRY,
    WORKSPACE_MODEL_CONFIG_ENV,
    LocalOnlyUnavailable,
    ModelProfile,
    RoutingDimension,
    RoutingRule,
    WorkspaceModelConfig,
    configured_local_model,
    is_local_endpoint,
    is_local_model,
    local_gateway_url,
    provider_identity,
    route_decision,
)

ASK_LOGGER = "knowledge_os.api.ask"
_LOCAL_MODEL = "llama3.1:8b"
_SAFE_UNAVAILABLE_MESSAGE = "no local model is configured for this local-only workspace"
#: Planting these in env must never change a local-only outcome — they are the
#: EXTERNAL gateway configuration this ticket fences off.
_EXTERNAL_URL = "https://openrouter.ai/api/v1"


def _no_local_model_configured(monkeypatch) -> None:
    """Explicit precondition of the fail-closed cases: nothing local allowed."""
    monkeypatch.delenv(LOCAL_ONLY_MODEL_ENV, raising=False)
    assert configured_local_model() is None, (
        "this tree registers no local=True profile; the env declaration is "
        "the only way to allow a local model"
    )


# ---------------------------------------------------------------------------
# Dummy transports (no real network — ever)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.text = json.dumps(payload)
        self.status_code = 200

    def raise_for_status(self) -> None:  # pragma: no cover - always 200 in tests
        pass

    def json(self) -> dict:
        return self._payload


class _RecordingClient:
    """In-memory stand-in for httpx.AsyncClient: records every transport call."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.streams: list[tuple[str, str, dict]] = []

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self.posts.append((url, kwargs))
        return _FakeResponse(
            {
                "id": "x",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            }
        )

    def stream(self, method: str, url: str, **kwargs):  # pragma: no cover - guarded
        self.streams.append((method, url, kwargs))
        raise AssertionError("stream transport must not be reached in these tests")


def _gen_request(model: str) -> GenerationRequest:
    return GenerationRequest(
        model=model, messages=[ChatMessage(role=MessageRole.USER, content="hi")]
    )


# ---------------------------------------------------------------------------
# 1. Policy: local-only resolves to a local model or fails closed
# ---------------------------------------------------------------------------


def test_sensitive_without_local_model_fails_closed(monkeypatch):
    """The ticket's instruction 1, fail-closed half: no explicitly allowed
    local model ⇒ documented unavailable, never the sentinel label and never
    a cloud model."""
    _no_local_model_configured(monkeypatch)
    config = WorkspaceModelConfig(workspace_id="ws-tv1-029", sensitive=True)
    for query_class in ("simple", "complex", "code", "multilingual"):
        with pytest.raises(LocalOnlyUnavailable) as excinfo:
            route_decision(query_class, config)
        assert str(excinfo.value) == _SAFE_UNAVAILABLE_MESSAGE


def test_sensitive_resolves_to_the_declared_local_model(monkeypatch):
    """The ticket's instruction 1, resolve half: the veto beats override,
    workspace rules and operator default, and resolves to a REAL local model."""
    monkeypatch.setenv(LOCAL_ONLY_MODEL_ENV, _LOCAL_MODEL)
    config = WorkspaceModelConfig(
        workspace_id="ws-tv1-029",
        sensitive=True,
        override_model="request-override-cloud",
        preferred_model="operator-default-cloud",
        routing_rules=(RoutingRule(RoutingDimension.COST, "low", priority=1),),
    )
    for query_class in ("simple", "complex", "code", "multilingual"):
        model, reason = route_decision(query_class, config)
        assert model == _LOCAL_MODEL
        assert model != LOCAL_ONLY_SENTINEL
        assert reason == "policy_veto_local_only"
    assert is_local_model(_LOCAL_MODEL)


def test_the_sentinel_is_never_a_routable_model(monkeypatch):
    """Sweep: no configuration class may return the sentinel as a model."""
    monkeypatch.delenv(LOCAL_ONLY_MODEL_ENV, raising=False)
    # Non-sensitive tiers (override / preferred / fallback).
    for config in (
        WorkspaceModelConfig(workspace_id="w", override_model="override-x"),
        WorkspaceModelConfig(workspace_id="w", preferred_model="preferred-x"),
        WorkspaceModelConfig(workspace_id="w"),
    ):
        model, _reason = route_decision("simple", config)
        assert model != LOCAL_ONLY_SENTINEL
    # Sensitive + declared local model.
    monkeypatch.setenv(LOCAL_ONLY_MODEL_ENV, _LOCAL_MODEL)
    model, _reason = route_decision(
        "simple", WorkspaceModelConfig(workspace_id="w", sensitive=True)
    )
    assert model == _LOCAL_MODEL != LOCAL_ONLY_SENTINEL
    # Sensitive without one raises — it cannot fall through to a model.
    monkeypatch.delenv(LOCAL_ONLY_MODEL_ENV, raising=False)
    with pytest.raises(LocalOnlyUnavailable):
        route_decision("simple", WorkspaceModelConfig(workspace_id="w", sensitive=True))
    # The sentinel itself is never classified as a local (or any) model.
    assert is_local_model(LOCAL_ONLY_SENTINEL) is False


def test_configured_local_model_resolution_order(monkeypatch):
    """Explicit declaration first, cheapest local registry profile second,
    ``None`` (fail closed) when neither exists. Workspace ``extra_models``
    deliberately do NOT qualify: transport locality is operator-level config."""
    monkeypatch.delenv(LOCAL_ONLY_MODEL_ENV, raising=False)
    assert configured_local_model() is None  # this tree ships zero local profiles
    monkeypatch.setitem(
        MODEL_REGISTRY,
        "test-local-b",
        ModelProfile(name="test-local-b", cost="medium", local=True),
    )
    monkeypatch.setitem(
        MODEL_REGISTRY,
        "test-local-a",
        ModelProfile(name="test-local-a", cost="medium", local=True),
    )
    assert configured_local_model() == "test-local-a"  # deterministic tie-break
    monkeypatch.setenv(LOCAL_ONLY_MODEL_ENV, _LOCAL_MODEL)
    assert configured_local_model() == _LOCAL_MODEL  # env declaration wins


def test_local_endpoint_classification_is_explicit(monkeypatch):
    """Loopback is local; every other host is egress until explicitly allowed."""
    for url in ("http://localhost:4000", "http://127.0.0.1:11434/v1", "https://[::1]:8000"):
        assert is_local_endpoint(url) is True, url
    for url in (_EXTERNAL_URL, "https://api.example.com", "http://10.0.0.5:4000", "", "nonsense"):
        assert is_local_endpoint(url) is False, url
    # An operator can EXPLICITLY allow a non-loopback local endpoint.
    monkeypatch.setenv(LOCAL_GATEWAY_EXTRA_HOSTS_ENV, "vllm.internal.lan")
    assert is_local_endpoint("http://vllm.internal.lan:8000/v1") is True
    assert is_local_endpoint(_EXTERNAL_URL) is False  # allowlist is not a wildcard


def test_local_gateway_url_default_and_override(monkeypatch):
    monkeypatch.delenv(LOCAL_GATEWAY_URL_ENV, raising=False)
    assert local_gateway_url() == DEFAULT_LOCAL_GATEWAY_URL
    assert is_local_endpoint(local_gateway_url()) is True  # the default is local
    monkeypatch.setenv(LOCAL_GATEWAY_URL_ENV, "http://127.0.0.1:11434/v1")
    assert local_gateway_url() == "http://127.0.0.1:11434/v1"


# ---------------------------------------------------------------------------
# 2. Provider (the egress point): sentinel refused, local transport enforced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("base_url", [_EXTERNAL_URL, "http://127.0.0.1:4000"])
async def test_provider_refuses_to_post_the_sentinel_to_any_gateway(base_url):
    """Defense in depth (reviewer checklist 1): even handed the sentinel
    DIRECTLY — bypassing the policy — the provider posts it to NO gateway,
    external or local: zero transport invocations."""
    client = _RecordingClient()
    provider = LiteLLMProvider(base_url=base_url, client=client)
    with pytest.raises(LLMProviderError):
        await provider.generate(_gen_request(LOCAL_ONLY_SENTINEL))
    assert client.posts == []
    with pytest.raises(LLMProviderError):
        _ = [chunk async for chunk in provider.stream(_gen_request(LOCAL_ONLY_SENTINEL))]
    assert client.streams == []


def test_local_transport_fails_closed_when_the_endpoint_is_not_local(monkeypatch):
    """Explicit unavailable half of the transport seam: ``require_local``
    refuses to even construct a provider on a non-local endpoint — before a
    client exists, so nothing can egress."""
    with pytest.raises(LLMProviderError):
        LiteLLMProvider(base_url=_EXTERNAL_URL, require_local=True)
    # Via the env knob too (the factory path): misconfiguration fails closed.
    monkeypatch.setenv(LOCAL_GATEWAY_URL_ENV, _EXTERNAL_URL)
    with pytest.raises(LLMProviderError):
        LiteLLMProvider(base_url=local_gateway_url(), require_local=True)
    # Loopback (and an explicitly allowed host) construct fine.
    assert LiteLLMProvider(base_url="http://127.0.0.1:11434/v1", require_local=True)
    monkeypatch.setenv(LOCAL_GATEWAY_EXTRA_HOSTS_ENV, "vllm.internal.lan")
    assert LiteLLMProvider(base_url="http://vllm.internal.lan:8000", require_local=True)


async def test_local_transport_posts_only_to_the_local_endpoint():
    """Local model ⇒ the ONLY transport that ever sees the request is the
    explicitly local endpoint."""
    client = _RecordingClient()
    provider = LiteLLMProvider(
        base_url="http://127.0.0.1:11434/v1", require_local=True, client=client
    )
    result = await provider.generate(_gen_request(_LOCAL_MODEL))
    assert result.text == "ok"
    assert len(client.posts) == 1
    url, kwargs = client.posts[0]
    assert is_local_endpoint(url)
    assert url.startswith("http://127.0.0.1:11434/")
    assert kwargs["json"]["model"] == _LOCAL_MODEL != LOCAL_ONLY_SENTINEL


# ---------------------------------------------------------------------------
# 3. Factory: transport follows the policy's local classification
# ---------------------------------------------------------------------------


class _DummyOrchestrator:
    """Build-time stand-in; no service is ever contacted in these tests."""


def _build_engine(monkeypatch, model: str):
    from answer import factory as factory_mod

    monkeypatch.setattr(factory_mod, "create_orchestrator", lambda **_: _DummyOrchestrator())
    return factory_mod.build_answer_engine(model=model)


def test_factory_serves_local_models_on_the_local_transport_even_with_external_defaults(
    monkeypatch,
):
    """The egress fence: even when the deployment IS configured for an
    external gateway (OPENROUTER_API_KEY flips the provider default to
    openrouter.ai, LITELLM_URL points there), a local model is built on the
    local endpoint only."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-planted-external")
    monkeypatch.setenv("LITELLM_URL", _EXTERNAL_URL)
    monkeypatch.setenv(LOCAL_ONLY_MODEL_ENV, _LOCAL_MODEL)
    monkeypatch.setenv(LOCAL_GATEWAY_URL_ENV, "http://127.0.0.1:11434/v1")
    engine = _build_engine(monkeypatch, _LOCAL_MODEL)
    assert engine.model == _LOCAL_MODEL
    assert isinstance(engine.llm, LiteLLMProvider)
    assert engine.llm.base_url == "http://127.0.0.1:11434/v1"
    assert is_local_endpoint(engine.llm.base_url)


def test_factory_fails_closed_when_the_local_endpoint_is_not_local(monkeypatch):
    """No local endpoint ⇒ no provider, no client, documented unavailable."""
    monkeypatch.setenv(LOCAL_ONLY_MODEL_ENV, _LOCAL_MODEL)
    monkeypatch.setenv(LOCAL_GATEWAY_URL_ENV, _EXTERNAL_URL)
    with pytest.raises(LLMProviderError):
        _build_engine(monkeypatch, _LOCAL_MODEL)


def test_factory_keeps_the_cloud_path_for_cloud_models(monkeypatch):
    """The local-only fence does not disturb normal routing: a cloud model
    keeps the existing gateway default resolution, unchanged."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_URL", _EXTERNAL_URL)
    engine = _build_engine(monkeypatch, "gpt-4o-mini")
    assert engine.llm.base_url == _EXTERNAL_URL


# ---------------------------------------------------------------------------
# 4. Sensitive fixture: the REAL handler, local transport or explicit error
# ---------------------------------------------------------------------------


class _FakeEngine:
    def __init__(self) -> None:
        self.model: str | None = None

    async def answer(self, *, query, acl_scope, mode=None, **kwargs) -> Answer:
        return Answer(answer="ok")


class _FactoryRecorder:
    """Stands in for build_answer_engine and records every engine build.

    If the handler ever builds an engine for a local-only request, THIS is
    where the (dummy) gateway would live — so ``models == []`` below is the
    "external gateway never invoked" assertion at the handler seam; the real
    transport wiring those engines would use is pinned in section 3.
    """

    def __init__(self) -> None:
        self.models: list[str] = []
        self.engine = _FakeEngine()

    def __call__(self, *, model: str) -> _FakeEngine:
        self.models.append(model)
        self.engine.model = model
        return self.engine


def _events(caplog) -> list:
    return [r for r in caplog.records if getattr(r, "event", None) == "ask_model_selected"]


def _sensitive_workspace(monkeypatch, recorder: _FactoryRecorder) -> uuid.UUID:
    """Wire ONE sensitive workspace through the REAL store + handler seam.

    Plants external-gateway configuration in every case: it must never win.
    """
    import app.ask.router as router_mod

    workspace_id = uuid.uuid4()
    store = {str(workspace_id): {"sensitive": True}}
    monkeypatch.setenv(WORKSPACE_MODEL_CONFIG_ENV, json.dumps(store))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-planted-external")
    monkeypatch.setenv("LITELLM_URL", _EXTERNAL_URL)
    monkeypatch.setattr(router_mod, "build_answer_engine", recorder)
    return workspace_id


def _principal(workspace_id: uuid.UUID) -> Principal:
    return Principal(
        user_id="u-tv1-029",
        is_authenticated=True,
        workspace_roles={str(workspace_id): Role.OWNER},
    )


async def test_local_only_request_fails_closed_with_documented_unavailable(monkeypatch, caplog):
    """Sensitive fixture, case A (ticket instruction 2): the explicit error —
    503, registered wire value, fixed safe message, tracking ID, and NO engine
    build (so no gateway client could exist; external invocations: 0)."""
    _no_local_model_configured(monkeypatch)
    recorder = _FactoryRecorder()
    workspace_id = _sensitive_workspace(monkeypatch, recorder)
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)

    with pytest.raises(HTTPException) as excinfo:
        await ask(
            workspace_id,
            AskRequest(query="What is the confidential budget?"),
            _principal(workspace_id),
        )

    exc = excinfo.value
    assert exc.status_code == 503
    assert exc.detail["error"] == "llm_unavailable"  # registered wire value
    assert exc.detail["message"] == _SAFE_UNAVAILABLE_MESSAGE
    assert "trace_id" in exc.detail
    # The external gateway was NEVER invoked: no engine (hence no client) was
    # ever built, and the sentinel never left the policy layer.
    assert recorder.models == []


async def test_local_only_request_uses_the_local_transport(monkeypatch, caplog):
    """Sensitive fixture, case B (ticket instruction 2): with a local model
    explicitly allowed, the handler proceeds with THAT model (never the
    sentinel, never a cloud model) and records provider=local; the factory
    (section 3) can only build the local transport for it — so the external
    gateway is never invoked here either."""
    monkeypatch.setenv(LOCAL_ONLY_MODEL_ENV, _LOCAL_MODEL)
    monkeypatch.setenv(LOCAL_GATEWAY_URL_ENV, "http://127.0.0.1:11434/v1")
    recorder = _FactoryRecorder()
    workspace_id = _sensitive_workspace(monkeypatch, recorder)
    caplog.set_level(logging.INFO, logger=ASK_LOGGER)

    answer = await ask(
        workspace_id,
        AskRequest(query="What is the confidential budget?"),
        _principal(workspace_id),
    )

    assert answer.answer == "ok"
    assert recorder.models == [_LOCAL_MODEL]  # one build, the LOCAL model
    assert recorder.models[0] != LOCAL_ONLY_SENTINEL
    events = _events(caplog)
    assert len(events) == 1
    assert events[0].model == _LOCAL_MODEL
    assert events[0].provider == "local"
    assert events[0].reason == "policy_veto_local_only"


def test_load_model_config_wires_sensitive_from_the_store(monkeypatch):
    """The store's ``sensitive`` flag is the production opt-in for the veto
    (strict JSON boolean — a string is not an opt-in)."""
    ws = str(uuid.uuid4())
    monkeypatch.setenv(WORKSPACE_MODEL_CONFIG_ENV, json.dumps({ws: {"sensitive": True}}))
    assert load_model_config(ws, override_model=None).sensitive is True
    monkeypatch.setenv(WORKSPACE_MODEL_CONFIG_ENV, json.dumps({ws: {"sensitive": "true"}}))
    assert load_model_config(ws, override_model=None).sensitive is False
    monkeypatch.setenv(WORKSPACE_MODEL_CONFIG_ENV, json.dumps({ws: {}}))
    assert load_model_config(ws, override_model=None).sensitive is False
    monkeypatch.delenv(WORKSPACE_MODEL_CONFIG_ENV, raising=False)
    assert load_model_config(ws, override_model=None).sensitive is False


def test_provider_identity_labels_local_models(monkeypatch):
    """Observability: a local-only request logs provider=local — and the label
    never leaks host/URL/credential material."""
    monkeypatch.setenv(LOCAL_ONLY_MODEL_ENV, _LOCAL_MODEL)
    assert provider_identity(_LOCAL_MODEL) == "local"
    for model in ("gpt-4o-mini", "openai/gpt-4o-mini", "unknown-model"):
        ident = provider_identity(model)
        assert ident in {"litellm", "openrouter"}
        assert "http" not in ident and "key" not in ident.lower()
