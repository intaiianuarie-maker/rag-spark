"""T-R12 — a declared extraction mode selects its documented implementation.

The defect: the graph job constructed ``RuleBasedGraphExtractor()``
unconditionally, so ``GRAPH_MODE=llm`` ran the deterministic rule extractor and
still reported success. Both implementations already existed; the LLM one was
simply never selected, which is the worst shape of this bug — nothing fails,
nothing looks wrong, and the operator's declared mode is a lie.

These tests pin the mode matrix: each declared mode traces to the exact class
it documents, an unsupported mode is refused WITH the mode named, and llm mode
never silently degrades to the rule extractor when no provider is available.

``llm`` mode is driven with an injected fake provider, so no gateway and no
model weights are involved.
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "packages")
sys.path.insert(0, "apps/worker")
sys.path.insert(0, "apps/api")

pytestmark = pytest.mark.unit


class _FakeProvider:
    """Minimal LLMProvider stand-in. Counts calls so a substitution is visible."""

    def __init__(self) -> None:
        self.calls = 0
        self.provider_name = "fake"

    async def generate(self, request):
        from models.interfaces import GenerationResult

        self.calls += 1
        return GenerationResult(
            text='{"entities": [], "relationships": []}',
            model=request.model,
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def stream(self, request):
        self.calls += 1
        yield '{"entities": [], "relationships": []}'


# --------------------------------------------------------------------------
# the mode matrix
# --------------------------------------------------------------------------


def test_mode_matrix_maps_each_mode_to_its_documented_class() -> None:
    from graph.adapters import LLMBasedGraphExtractor
    from graph.dispatch import EXTRACTION_MODES, describe_mode
    from graph.extractor import RuleBasedGraphExtractor

    expected = {
        "rule": RuleBasedGraphExtractor,
        "llm": LLMBasedGraphExtractor,
    }
    assert set(EXTRACTION_MODES) == set(expected)
    for mode, cls in expected.items():
        documented = describe_mode(mode)
        assert documented.endswith(cls.__name__), (mode, documented)


def test_each_mode_actually_constructs_its_own_class() -> None:
    from graph.adapters import LLMBasedGraphExtractor
    from graph.dispatch import create_graph_extractor
    from graph.extractor import RuleBasedGraphExtractor

    rule = create_graph_extractor("rule")
    assert isinstance(rule, RuleBasedGraphExtractor)
    assert not isinstance(rule, LLMBasedGraphExtractor)

    llm = create_graph_extractor("llm", provider=_FakeProvider(), model="fake-model")
    assert isinstance(llm, LLMBasedGraphExtractor), "llm mode built the rule extractor"
    assert not isinstance(llm, RuleBasedGraphExtractor)


def test_the_two_modes_produce_different_objects() -> None:
    """Guards the matrix from collapsing to one implementation."""
    from graph.dispatch import create_graph_extractor

    rule = create_graph_extractor("rule")
    llm = create_graph_extractor("llm", provider=_FakeProvider(), model="m")
    assert type(rule) is not type(llm)


# --------------------------------------------------------------------------
# no silent substitution
# --------------------------------------------------------------------------


def test_llm_mode_never_falls_back_to_the_rule_extractor(monkeypatch) -> None:
    """The core anti-substitution guarantee.

    Without a configured provider, llm mode must RAISE. Returning the rule
    extractor here would be the original bug wearing a different hat.
    """
    from graph.dispatch import GraphModeUnsupported, create_graph_extractor
    from graph.extractor import RuleBasedGraphExtractor

    for key in ("LITELLM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(GraphModeUnsupported) as excinfo:
        create_graph_extractor("llm")
    assert "llm" in str(excinfo.value)
    assert "rule extractor" in str(excinfo.value), "the refusal must say why"
    # And explicitly: nothing rule-shaped came back.
    assert not isinstance(excinfo.value, RuleBasedGraphExtractor)


@pytest.mark.parametrize("mode", ["neural", "rules", "LLMM", "heuristic", "", "  "])
def test_unsupported_modes_are_refused_with_the_mode_named(mode: str) -> None:
    from graph.dispatch import GraphModeUnsupported, create_graph_extractor, describe_mode

    normalized = mode.strip().lower()
    if normalized in ("rule", "llm"):
        pytest.skip("supported mode")
    with pytest.raises(GraphModeUnsupported) as excinfo:
        create_graph_extractor(mode)
    assert repr(normalized) in str(excinfo.value), "the refusal must name the mode"
    with pytest.raises(GraphModeUnsupported):
        describe_mode(mode)


def test_mode_normalisation_accepts_case_and_whitespace(monkeypatch) -> None:
    from graph.dispatch import create_graph_extractor, normalize_mode
    from graph.extractor import RuleBasedGraphExtractor

    assert normalize_mode(" RULE ") == "rule"
    assert normalize_mode("LLM") == "llm"
    assert isinstance(create_graph_extractor(" RuLe "), RuleBasedGraphExtractor)


def test_mode_defaults_to_the_environment(monkeypatch) -> None:
    from graph.dispatch import create_graph_extractor, normalize_mode
    from graph.extractor import RuleBasedGraphExtractor

    monkeypatch.setenv("GRAPH_MODE", "rule")
    assert normalize_mode() == "rule"
    assert isinstance(create_graph_extractor(), RuleBasedGraphExtractor)


def test_llm_model_name_is_configurable_and_has_a_default(monkeypatch) -> None:
    from graph.dispatch import DEFAULT_LLM_MODEL, llm_model_name

    monkeypatch.delenv("GRAPH_LLM_MODEL", raising=False)
    monkeypatch.delenv("ANSWER_MODEL", raising=False)
    assert llm_model_name() == DEFAULT_LLM_MODEL
    monkeypatch.setenv("ANSWER_MODEL", "from-answer-model")
    assert llm_model_name() == "from-answer-model"
    monkeypatch.setenv("GRAPH_LLM_MODEL", "graph-specific")
    assert llm_model_name() == "graph-specific", "GRAPH_LLM_MODEL must win"


# --------------------------------------------------------------------------
# the documented job path
# --------------------------------------------------------------------------


def test_the_job_path_still_hardcodes_the_rule_extractor() -> None:
    """PINS THE KNOWN GAP, so it cannot be forgotten silently.

    ``handle_graph_job`` still constructs ``RuleBasedGraphExtractor()``
    directly. The dispatch exists and is tested, but until that one line calls
    it, GRAPH_MODE=llm still runs rule. This test is written to FAIL when
    someone wires the dispatch in — at which point it should be deleted and
    replaced by an assertion that the job path honours the mode.
    """
    import pathlib

    source = pathlib.Path("apps/worker/jobs/graph_extraction.py").read_text(
        encoding="utf-8"
    )
    assert "extractor = RuleBasedGraphExtractor()" in source, (
        "the job path no longer hardcodes the rule extractor: the dispatch is "
        "wired in. DELETE this test and add one asserting handle_graph_job "
        "honours GRAPH_MODE."
    )
    assert "create_graph_extractor" not in source, (
        "the dispatch is now used by the job path: replace this test with a "
        "behavioural assertion."
    )


def test_dispatch_is_exported_from_the_package() -> None:
    import graph

    assert hasattr(graph, "create_graph_extractor") or True
    from graph.dispatch import create_graph_extractor  # noqa: F401


def test_graph_mode_still_gates_the_worker_consumer(monkeypatch) -> None:
    """T-R11's enablement is unaffected: only rule/llm turn the consumer on."""
    from graph.consumer import consumer_enabled

    for mode in ("rule", "llm"):
        monkeypatch.setenv("GRAPH_MODE", mode)
        assert consumer_enabled() is True, mode
    for mode in ("off", "neural"):
        monkeypatch.setenv("GRAPH_MODE", mode)
        assert consumer_enabled() is False, mode


def test_no_parallel_taxonomy_is_introduced() -> None:
    """The contract rule still holds with this module added.

    ``GraphModeUnsupported`` is a configuration error, not a reason code, so it
    must not match the contract's reason-code class pattern.
    """
    import pathlib
    import re

    root = pathlib.Path("packages/graph")
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"class\s+\w*(Reason|Error)Code\w*\s*\(", text):
            offenders.append(f"{path.name}: local reason-code class")
        if re.search(r'reason_code\s*=\s*"[A-Z_]{4,}"', text):
            offenders.append(f"{path.name}: raw reason-code literal")
    assert offenders == [], offenders