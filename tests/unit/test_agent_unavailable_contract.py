"""T-R02 — an unconfigured agent/research request can NEVER look completed.

PRIO1 findings 2 and 3. Before this ticket:

  * ``app.agents.router.create_runtime()`` built a ``_NoopLLM`` whose
    ``generate()`` returned the literal text ``"Final Answer: done"`` for ANY
    goal — so asking "what is 17x19" returned a confident fabricated answer;
  * ``app.agents.research`` had no real pipeline, so it manufactured chunk
    ``chk-1``, evidence ``ev-1``, a grounded sentence echoing the topic, and
    unknowns/contradictions triggered purely by the words "unknown"/"conflict"
    appearing in the topic — a report that cited evidence never retrieved.

Both paths now return a typed UNAVAILABLE that names the missing piece and
makes no provider/tool call. Test doubles remain possible, but only behind
explicit test-only construction: the production factories have no fake to fall
back on.

The zero-call assertions carry a NON-VACUITY control: the same spies are shown
detecting real calls on a configured path, so "zero calls" cannot pass simply
because the spy was never wired up.
"""

from __future__ import annotations

import ast
import os
import pathlib
import uuid
from typing import ClassVar

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

pytestmark = pytest.mark.unit

WS_ID = str(uuid.uuid4())

ROUTER_SRC = pathlib.Path("apps/api/app/agents/router.py")
RESEARCH_SRC = pathlib.Path("apps/api/app/agents/research.py")

#: Env keys the production provider resolver consults. Cleared so the tests do
#: not depend on whether a real gateway happens to be configured on the host.
_PROVIDER_ENV = ("LITELLM_URL", "LITELLM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")


def _principal(user_id: str = "user-1"):
    from core.auth import Principal, Role

    return Principal(
        user_id=user_id,
        email=f"{user_id}@example.test",
        is_authenticated=True,
        workspace_roles={WS_ID: Role.EDITOR},
    )


def _client(monkeypatch, *, unconfigured: bool = True):
    """Build a TestClient. By default the production factories are UNPATCHED,
    which is the path under test."""
    from app.authz.principal import get_principal
    from app.main import create_app
    from fastapi.testclient import TestClient

    if unconfigured:
        for key in _PROVIDER_ENV:
            monkeypatch.delenv(key, raising=False)
    app = create_app()
    app.dependency_overrides[get_principal] = lambda: _principal()
    return TestClient(app)


class _SpyProvider:
    """Counts every LLM interaction. Never actually called on the tested path."""

    calls: ClassVar[list[str]] = []

    @classmethod
    def reset(cls) -> None:
        cls.calls = []

    @classmethod
    def count(cls) -> int:
        return len(cls.calls)


def _spy_tool_registry(monkeypatch) -> dict:
    """Wrap ToolRegistry so registrations are counted."""
    from agents import tools as agents_tools

    real = agents_tools.ToolRegistry
    counter = {"register": 0}

    class SpyRegistry(real):  # type: ignore[misc, valid-type]
        def register(self, tool):
            counter["register"] += 1
            return super().register(tool)

    monkeypatch.setattr("app.agents.router.ToolRegistry", SpyRegistry)
    return counter


def _spy_provider_factory(monkeypatch) -> dict:
    """Count attempts to build a REAL provider."""
    counter = {"builds": 0}

    class Boom(RuntimeError):
        pass

    def fake_build(**kwargs):
        counter["builds"] += 1
        raise Boom("provider factory should not be reached when unconfigured")

    monkeypatch.setattr(
        "models.adapters.litellm_provider.create_llm_provider", fake_build, raising=False
    )
    return counter


# --------------------------------------------------------------------------
# METRIC 1 — unconfigured completions: 0
# --------------------------------------------------------------------------


def test_17x19_run_does_not_return_done(monkeypatch) -> None:
    """The named probe: an arithmetic goal must never come back 'completed'."""
    client = _client(monkeypatch)
    resp = client.post(
        f"/v1/workspaces/{WS_ID}/agent/runs", json={"goal": "what is 17x19?"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] != "completed", body
    assert body["status"] == "failed", body
    assert body["error_code"] == "unavailable", body
    assert "llm_provider" in (body["error"] or ""), body


def test_unavailable_run_is_polled_consistently(monkeypatch) -> None:
    """The stored record must agree with the POST response, so polling a dead
    run reports unavailable rather than a different story."""
    client = _client(monkeypatch)
    start = client.post(
        f"/v1/workspaces/{WS_ID}/agent/runs", json={"goal": "what is 17x19?"}
    )
    run_id = start.json()["run_id"]
    status = client.get(f"/v1/workspaces/{WS_ID}/agent/runs/{run_id}")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["status"] == "failed", body
    assert body["error_code"] == "unavailable", body


def test_noop_llm_cannot_return_final_answer_done(monkeypatch) -> None:
    """No production code path may contain a hardcoded completion string."""
    client = _client(monkeypatch)
    for goal in ("what is 17x19?", "summarise the contract", "list files"):
        body = client.post(
            f"/v1/workspaces/{WS_ID}/agent/runs", json={"goal": goal}
        ).json()
        assert body["status"] == "failed", (goal, body)
        assert body["error_code"] == "unavailable", (goal, body)


def test_research_unconfigured_never_returns_chk_1(monkeypatch) -> None:
    """The invented-budget probe: unavailable, never chk-1."""
    client = _client(monkeypatch)
    resp = client.post(
        f"/v1/workspaces/{WS_ID}/agent/research", json={"topic": "invented budget of 900"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] != "completed", resp.text
    assert resp.json()["error_code"] == "unavailable", resp.text

    report = client.get(
        f"/v1/workspaces/{WS_ID}/agent/research/{resp.json()['research_id']}/report"
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["citations"] == [], body
    assert body["grounded_sentences"] == [], body
    assert body["unknowns"] == [], body
    assert body["contradictions"] == [], body
    assert body["missing"] == "research_pipeline", body


@pytest.mark.parametrize(
    "topic",
    [
        "unknown budget",
        "conflict of interest",
        "unknown and conflict",
        "What is the unknown?",
        "any CONFLICT here",
    ],
)
def test_keywords_never_manufacture_unknowns_or_contradictions(
    monkeypatch, topic: str
) -> None:
    """The keyword triggers are gone: content words cannot conjure evidence."""
    client = _client(monkeypatch)
    created = client.post(
        f"/v1/workspaces/{WS_ID}/agent/research", json={"topic": topic}
    ).json()
    report = client.get(
        f"/v1/workspaces/{WS_ID}/agent/research/{created['research_id']}/report"
    ).json()
    assert report["unknowns"] == [], (topic, report)
    assert report["contradictions"] == [], (topic, report)
    assert report["citations"] == [], (topic, report)
    assert report["abstained"] is True, (topic, report)


# --------------------------------------------------------------------------
# METRIC 2 — provider/tool calls from unavailable paths: 0
# --------------------------------------------------------------------------


def test_unavailable_run_builds_no_provider_and_no_tool(monkeypatch) -> None:
    registry = _spy_tool_registry(monkeypatch)
    provider = _spy_provider_factory(monkeypatch)
    _SpyProvider.reset()

    client = _client(monkeypatch)
    body = client.post(
        f"/v1/workspaces/{WS_ID}/agent/runs", json={"goal": "what is 17x19?"}
    ).json()
    assert body["error_code"] == "unavailable", body
    assert registry["register"] == 0, "a tool was registered on an unavailable path"
    assert provider["builds"] == 0, "a provider factory was called when unconfigured"
    assert _SpyProvider.count() == 0


def test_unavailable_research_writes_no_chunks(monkeypatch) -> None:
    """InMemoryChunkSource.put is how chk-1 used to be fabricated."""
    from agents import verifier as agents_verifier

    counter = {"put": 0}
    real_put = agents_verifier.InMemoryChunkSource.put

    def counting_put(self, *args, **kwargs):
        counter["put"] += 1
        return real_put(self, *args, **kwargs)

    monkeypatch.setattr(agents_verifier.InMemoryChunkSource, "put", counting_put)

    client = _client(monkeypatch)
    created = client.post(
        f"/v1/workspaces/{WS_ID}/agent/research", json={"topic": "invented budget of 900"}
    ).json()
    assert created["error_code"] == "unavailable", created
    assert counter["put"] == 0, "the unavailable path wrote into the chunk source"


def test_the_spies_can_detect_calls_so_zero_is_not_vacuous(monkeypatch) -> None:
    """NON-VACUITY CONTROL.

    A spy that is never wired up proves nothing. This runs a CONFIGURED path
    through the same instrumentation and shows the counters move — provider
    generate() called, tools registered — so the zeros above are meaningful.
    """
    from collections.abc import AsyncIterator

    from agents.runtime import AgentRuntime
    from agents.tools import CalculatorTool, ToolRegistry
    from models.interfaces import GenerationRequest, GenerationResult, LLMProvider

    _SpyProvider.reset()

    class SpyLLM(LLMProvider):
        provider_name = "spy"

        async def generate(self, request: GenerationRequest) -> GenerationResult:
            _SpyProvider.calls.append("generate")
            return GenerationResult(
                text="Final Answer: 323", model=request.model,
                prompt_tokens=1, completion_tokens=1,
            )

        async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
            _SpyProvider.calls.append("stream")
            yield ""

    registry_counter = {"register": 0}
    real_register = ToolRegistry.register

    def counting_register(self, tool):
        registry_counter["register"] += 1
        return real_register(self, tool)

    monkeypatch.setattr(ToolRegistry, "register", counting_register)

    reg = ToolRegistry()
    reg.register(CalculatorTool())
    runtime = AgentRuntime(SpyLLM(), reg)

    def factory():
        return runtime

    monkeypatch.setattr("app.agents.router.create_runtime", factory)
    for key in _PROVIDER_ENV:
        monkeypatch.setenv(key, "set-for-the-control")

    client = _client(monkeypatch, unconfigured=False)
    client.post(f"/v1/workspaces/{WS_ID}/agent/runs", json={"goal": "what is 17x19?"})

    assert registry_counter["register"] == 1, "the registry spy cannot see registrations"
    assert _SpyProvider.count() >= 1, "the provider spy cannot see generate() calls"


# --------------------------------------------------------------------------
# production source cannot reach the removed behaviour
# --------------------------------------------------------------------------


def _literals(path: pathlib.Path) -> set[str]:
    """Every string literal in a module, via AST (comments excluded).

    Text scanning would be defeated by this ticket's own comments, which quote
    the removed strings on purpose to explain what was taken out.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_production_source_has_no_noop_llm_and_no_completion_string() -> None:
    for path in (ROUTER_SRC, RESEARCH_SRC):
        literals = _literals(path)
        assert "Final Answer: done" not in literals, path
        assert "chk-1" not in literals, path
        assert "ev-1" not in literals, path


def test_production_source_defines_no_noop_provider_class() -> None:
    tree = ast.parse(ROUTER_SRC.read_text(encoding="utf-8"))
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert not any("Noop" in name for name in classes), classes


def test_production_source_has_no_keyword_triggered_manufacture() -> None:
    """No `if "unknown" in topic` / `if "conflict" in topic` branch survives."""
    for path in (RESEARCH_SRC, ROUTER_SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            for operand in operands:
                if isinstance(operand, ast.Constant) and operand.value in {
                    "unknown",
                    "conflict",
                }:
                    if any(
                        isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
                    ):
                        raise AssertionError(
                            f"{path}: keyword-triggered manufacture survived: "
                            f"{operand.value!r}"
                        )


def test_create_runtime_raises_rather_than_substituting_a_fake(monkeypatch) -> None:
    from agents.unavailable import AgentUnavailableError
    from app.agents import router as agent_router

    for key in _PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)
    assert agent_router._resolve_llm_provider() is None
    with pytest.raises(AgentUnavailableError) as excinfo:
        agent_router.create_runtime()
    assert excinfo.value.missing == "llm_provider"
    assert "not configured" in str(excinfo.value) or "configured" in str(excinfo.value)


def test_unavailable_error_rejects_an_unknown_missing_piece() -> None:
    from agents.unavailable import AgentUnavailableError

    with pytest.raises(ValueError, match="unknown missing piece"):
        AgentUnavailableError("made_up_piece", "nope")


# --------------------------------------------------------------------------
# test-only construction still works
# --------------------------------------------------------------------------


def test_an_explicit_test_pipeline_can_stage_a_real_report(monkeypatch) -> None:
    """Fakes stay available — behind explicit construction, never as a default."""
    staged = {
        "grounded_sentences": [
            {
                "sentence": "The budget is 900 dollars.",
                "chunk_ids": ["chk-1"],
                "evidence_ids": ["ev-1"],
                "citations": ["chk-1"],
            }
        ],
        "unknowns": [
            {
                "question": "What is scope?",
                "searched": ["chk-x"],
                "reason": "NO_EVIDENCE",
                "tried_chunk_ids": [],
            }
        ],
        "contradictions": [
            {
                "question": "What is budget?",
                "claim_a": "900",
                "claim_b": "1900",
                "evidence_a_id": "a",
                "evidence_b_id": "b",
                "chunk_ids_a": ["chk-a"],
                "chunk_ids_b": ["chk-b"],
            }
        ],
        "contradiction_outline": ["900 [chk-a] vs 1900 [chk-b]"],
        "citations": ["chk-1"],
        "abstained": False,
        "abstention_reason": "",
        "searched": ["budget"],
        "limitations": [],
    }

    async def pipeline(topic, workspace_id, principal_id):
        return dict(staged)

    monkeypatch.setattr("app.agents.research.create_research_pipeline", lambda: pipeline)
    client = _client(monkeypatch)

    created = client.post(
        f"/v1/workspaces/{WS_ID}/agent/research", json={"topic": "budget"}
    ).json()
    assert created["status"] == "completed", created
    assert created["error_code"] is None, created

    report = client.get(
        f"/v1/workspaces/{WS_ID}/agent/research/{created['research_id']}/report"
    ).json()
    assert report["unknowns"] == staged["unknowns"]
    assert report["contradictions"] == staged["contradictions"]
    assert report["citations"] == ["chk-1"]
    assert report["grounded_sentences"][0]["evidence_ids"] == ["ev-1"]


def test_patched_run_factory_still_executes_normally(monkeypatch) -> None:
    """Configured path unchanged: a patched factory still produces a real run."""
    from collections.abc import AsyncIterator

    from agents.runtime import AgentRuntime
    from agents.tools import CalculatorTool, ToolRegistry
    from models.interfaces import GenerationRequest, GenerationResult, LLMProvider

    class FixedLLM(LLMProvider):
        provider_name = "fixed"

        async def generate(self, request: GenerationRequest) -> GenerationResult:
            return GenerationResult(
                text="Final Answer: 323", model=request.model,
                prompt_tokens=1, completion_tokens=1,
            )

        async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
            yield ""

    reg = ToolRegistry()
    reg.register(CalculatorTool())
    runtime = AgentRuntime(FixedLLM(), reg)
    monkeypatch.setattr("app.agents.router.create_runtime", lambda: runtime)

    client = _client(monkeypatch)
    body = client.post(
        f"/v1/workspaces/{WS_ID}/agent/runs", json={"goal": "what is 17x19?"}
    ).json()
    assert body["status"] != "unavailable", body
    assert body["error_code"] != "unavailable", body


# --------------------------------------------------------------------------
# the typed contract itself
# --------------------------------------------------------------------------


def test_unavailable_report_is_empty_and_names_the_missing_piece() -> None:
    from agents.unavailable import UNAVAILABLE_CODE, Unavailable

    report = Unavailable("research_pipeline", "not wired").report("a topic")
    assert report["error_code"] == UNAVAILABLE_CODE
    assert report["missing"] == "research_pipeline"
    assert report["abstained"] is True
    for key in ("grounded_sentences", "unknowns", "contradictions", "citations"):
        assert report[key] == [], key


def test_unavailable_code_is_a_member_of_the_typed_error_enum() -> None:
    from agents.interfaces import AgentErrorCode

    assert AgentErrorCode.UNAVAILABLE.value == "unavailable"


def test_unavailable_is_a_terminal_run_status_for_pollers() -> None:
    """The unavailable run must not be an ACTIVE state a poller waits on."""
    from agents.interfaces import RunStatus

    assert RunStatus.FAILED.value == "failed"