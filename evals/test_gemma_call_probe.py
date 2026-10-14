"""T-EVAL-009 — covering tests for the gemma function-call probe (offline).

These tests never touch the network: extraction, schema validation, the table
shape, the verdict arithmetic and the failure-recording discipline are all
exercised from recorded message fixtures. Run from the repo root:

    /root/ko-venv/bin/pytest evals/test_gemma_call_probe.py -q
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error

import pytest

EVALS = pathlib.Path(__file__).resolve().parent
REPO = EVALS.parent
for _p in (REPO / "packages", REPO / "apps" / "api", REPO, EVALS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gemma_call_probe as probe  # noqa: E402

CALL_CASE = probe.PROBE_PROMPTS[0]
SEARCH_CASE = next(c for c in probe.PROBE_PROMPTS if c.prompt_id == "search-with-topk")
READ_CASE = next(c for c in probe.PROBE_PROMPTS if c.prompt_id == "read-triple")
NO_CALL_CASE = next(c for c in probe.PROBE_PROMPTS if c.expected_tool is None)


# --------------------------------------------------------------------------- #
# The schemas are the REAL V3 ones, not invented
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_specs_are_the_real_v3_tool_specs() -> None:
    specs = probe.real_specs()
    assert set(specs) == {"calculator", "knowledge_search", "knowledge_read"}
    for spec in specs.values():
        assert spec.input_schema["type"] == "object"
        # Strict: the runtime pins object schemas to additionalProperties=false.
        assert spec.input_schema["additionalProperties"] is False
        assert spec.input_schema["required"]
    assert specs["calculator"].input_schema["properties"]["expression"]["type"] == "string"
    search = specs["knowledge_search"].input_schema["properties"]
    assert search["query"]["maxLength"] == 500
    assert search["top_k"]["type"] == "integer"
    assert (search["top_k"]["minimum"], search["top_k"]["maximum"]) == (1, 10)
    read = specs["knowledge_read"].input_schema["properties"]
    assert set(specs["knowledge_search"].input_schema["required"]) == {"query"}
    assert set(specs["knowledge_read"].input_schema["required"]) == {
        "document_id",
        "document_version_id",
        "chunk_id",
    }
    assert all(field["pattern"] == "^[0-9a-fA-F-]{36}$" for field in read.values())
    # No workspace_id smuggling: the reserved context fields are not properties.
    for spec in specs.values():
        assert "workspace_id" not in spec.input_schema["properties"]


@pytest.mark.unit
def test_specs_match_the_tools_own_declared_schema() -> None:
    """The probe derives specs via the runtime's own ``spec_for``."""
    from agents.tools import CalculatorTool

    specs = probe.real_specs()
    derived = specs["calculator"].input_schema
    assert derived["properties"] == CalculatorTool.input_schema["properties"]
    assert derived["required"] == CalculatorTool.input_schema["required"]
    # ... and only the documented strictness pin is added on top.
    assert CalculatorTool.input_schema.get("additionalProperties") is None


# --------------------------------------------------------------------------- #
# Assertion (a)+(b): a call is emitted, and it parses
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_native_tool_call_is_extracted() -> None:
    specs = probe.real_specs()
    message = {
        "content": None,
        "tool_calls": [
            {
                "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression": "1 + 1"}'},
            }
        ],
    }
    extracted = probe.extract_call(message, set(specs))
    assert extracted["emitted"] is True
    assert extracted["mode"] == "native"
    assert extracted["name"] == "calculator"
    assert probe.validate_arguments(specs["calculator"], extracted["arguments"]) == []


@pytest.mark.unit
def test_text_json_call_is_accepted_and_fences_stripped() -> None:
    specs = probe.real_specs()
    message = {
        "content": 'Sure!\n```json\n{"name": "knowledge_search", '
        '"arguments": {"query": "sla", "top_k": 3}}\n```',
        "tool_calls": [],
    }
    extracted = probe.extract_call(message, set(specs))
    assert extracted["emitted"] is True
    assert extracted["mode"] == "text_json"
    assert probe.validate_arguments(specs["knowledge_search"], extracted["arguments"]) == []


@pytest.mark.unit
def test_prose_about_calling_is_a_recorded_non_call() -> None:
    """The failure mode this probe exists to catch."""
    specs = probe.real_specs()
    message = {
        "content": "I will call the calculator tool with expression 12 * 45 + 10 "
        "and then report the answer.",
        "tool_calls": [],
    }
    extracted = probe.extract_call(message, set(specs))
    assert extracted["emitted"] is False
    assert extracted["mode"] == "prose_about_calling"
    assert "prose about calling" in extracted["error"]


@pytest.mark.unit
def test_plain_prose_is_a_recorded_non_call() -> None:
    specs = probe.real_specs()
    extracted = probe.extract_call({"content": "Paris is the capital."}, set(specs))
    assert extracted["emitted"] is False
    assert extracted["mode"] == "prose_only"
    assert extracted["error"]


@pytest.mark.unit
def test_unknown_tool_name_is_a_failure_not_a_pass() -> None:
    specs = probe.real_specs()
    message = {
        "content": '{"name": "web_search", "arguments": {"q": "x"}}',
        "tool_calls": [],
    }
    extracted = probe.extract_call(message, set(specs))
    assert extracted["emitted"] is False
    assert extracted["mode"] == "text_unknown_tool"
    assert "web_search" in extracted["error"]


@pytest.mark.unit
def test_unparseable_native_arguments_are_a_failure() -> None:
    specs = probe.real_specs()
    message = {
        "content": None,
        "tool_calls": [{"function": {"name": "calculator", "arguments": "{expression: 1+1"}}],
    }
    extracted = probe.extract_call(message, set(specs))
    assert extracted["emitted"] is False
    assert extracted["mode"] == "native_unparseable_arguments"


@pytest.mark.unit
def test_empty_response_is_a_recorded_non_call() -> None:
    specs = probe.real_specs()
    extracted = probe.extract_call({"content": "   "}, set(specs))
    assert extracted["emitted"] is False
    assert extracted["mode"] == "empty"


# --------------------------------------------------------------------------- #
# Assertion (c): arguments match the schema types
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_argument_type_mismatches_are_rejected() -> None:
    specs = probe.real_specs()
    search = specs["knowledge_search"].input_schema
    # top_k must be an integer, not "3".
    assert probe.validate_arguments(specs["knowledge_search"], {"query": "x", "top_k": "3"})
    # undeclared extra property (the runtime's strict pin).
    assert probe.validate_arguments(specs["knowledge_search"], {"query": "x", "workspace_id": "w"})
    # required field missing.
    assert probe.validate_arguments(specs["knowledge_search"], {})
    # wrong type entirely.
    assert probe.validate_arguments(specs["calculator"], {"expression": 42})
    # UUID pattern violated.
    assert probe.validate_arguments(specs["knowledge_read"], {
        "document_id": "not-a-uuid",
        "document_version_id": probe._UUID_B,
        "chunk_id": probe._UUID_C,
    })
    # arguments that are not an object at all.
    assert probe.validate_arguments(specs["calculator"], "expression=1+1")
    assert json.dumps(search["properties"]["top_k"]["type"]) == '"integer"'


@pytest.mark.unit
def test_valid_arguments_validate_clean() -> None:
    specs = probe.real_specs()
    assert probe.validate_arguments(specs["calculator"], {"expression": "2 ** 10 + 1"}) == []
    assert probe.validate_arguments(
        specs["knowledge_read"],
        {
            "document_id": probe._UUID_A,
            "document_version_id": probe._UUID_B,
            "chunk_id": probe._UUID_C,
        },
    ) == []


# --------------------------------------------------------------------------- #
# Reporting: per-prompt table, no dropped rows, verdict arithmetic
# --------------------------------------------------------------------------- #


def _result(**kwargs) -> probe.ProbeResult:
    defaults = dict(
        prompt_id="p",
        kind="call",
        expected_tool="calculator",
        model_used=probe.FREE_MODEL,
        mode="native",
        emitted=True,
        name_ok=True,
        args_ok=True,
        well_formed=True,
        arguments={"expression": "1 + 1"},
        errors=(),
        prompt_tokens=10,
        completion_tokens=2,
        cost_usd=0.0,
        latency_ms=5,
        fallback_reason="",
    )
    defaults.update(kwargs)
    return probe.ProbeResult(**defaults)


@pytest.mark.unit
def test_table_has_one_row_per_result_and_records_every_non_call() -> None:
    """silent non-calls: 0 — a failing prompt is a visible row with a reason."""
    results = [
        _result(prompt_id="ok"),
        _result(
            prompt_id="prose",
            emitted=False,
            name_ok=False,
            args_ok=False,
            well_formed=False,
            mode="prose_only",
            errors=("no call and no JSON in the response",),
        ),
        _result(
            prompt_id="wrong-tool",
            emitted=True,
            name_ok=False,
            well_formed=False,
            mode="native",
            expected_tool="calculator",
            errors=("called 'knowledge_search', expected 'calculator'",),
        ),
        _result(
            prompt_id="no-call-control",
            kind="no_call",
            expected_tool=None,
            emitted=False,
            name_ok=False,
            well_formed=False,
            mode="prose_only",
            errors=("no call and no JSON in the response",),
        ),
    ]
    table = probe.render_table(results)
    body = [line for line in table.splitlines()[2:] if line.strip()]
    assert len(body) == len(results)  # nothing dropped
    for result in results:
        assert result.prompt_id in table
        if not result.correct:
            assert result.errors, result.prompt_id
    summary = probe.summarize(results)
    assert summary["n_prompts"] == 4
    assert summary["n_call_expected"] == 3
    assert summary["n_well_formed"] == 1
    assert summary["n_failures"] == 2
    assert summary["n_negative_controls"] == 1
    assert summary["n_negative_controls_correct"] == 1
    assert summary["n_silent_non_calls"] == 0


@pytest.mark.unit
def test_verdict_uses_the_eighty_percent_bar() -> None:
    def rate(well_formed: int, total: int = 8) -> dict:
        results = [
            _result(prompt_id=f"p{i}", well_formed=i < well_formed, emitted=i < well_formed,
                    name_ok=i < well_formed, args_ok=i < well_formed,
                    mode="native" if i < well_formed else "prose_only",
                    errors=() if i < well_formed else ("no call and no JSON in the response",))
            for i in range(total)
        ]
        return probe.summarize(results)

    assert rate(8)["well_formed_rate"] == 1.0
    assert rate(8)["verdict"] == "PASS"
    # 7/8 = 0.875 and 4/5 = 0.8 both clear the bar; 6/8 = 0.75 does not.
    assert rate(7)["verdict"] == "PASS"
    assert rate(4, total=5)["well_formed_rate"] == 0.8
    assert rate(4, total=5)["verdict"] == "PASS"
    assert rate(6)["well_formed_rate"] == 0.75
    assert rate(6)["verdict"] == "BLOCKED"
    assert rate(6)["non_call_modes"] == ["prose_only"]


@pytest.mark.unit
def test_negative_controls_never_count_toward_the_bar() -> None:
    results = [
        _result(prompt_id=f"p{i}", well_formed=i < 7, emitted=i < 7, name_ok=i < 7,
                args_ok=i < 7, mode="native" if i < 7 else "prose_only",
                errors=() if i < 7 else ("no call and no JSON in the response",))
        for i in range(8)
    ]
    # Two controls that (correctly) emit nothing must not drag the rate down.
    results.append(_result(prompt_id="c1", kind="no_call", expected_tool=None, emitted=False,
                           name_ok=False, well_formed=False, mode="prose_only",
                           errors=("no call and no JSON in the response",)))
    results.append(_result(prompt_id="c2", kind="no_call", expected_tool=None, emitted=False,
                           name_ok=False, well_formed=False, mode="prose_only",
                           errors=("no call and no JSON in the response",)))
    summary = probe.summarize(results)
    assert summary["well_formed_rate"] == 0.875
    assert summary["verdict"] == "PASS"


# --------------------------------------------------------------------------- #
# Metering + transport failures are recorded, never dropped
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_pricing_and_fallback_spend_are_reported() -> None:
    assert probe._price_usd(probe.FREE_MODEL, 1_000_000, 1_000_000) == 0.0
    paid = probe._price_usd(probe.PAID_MODEL, 1_000_000, 1_000_000)
    assert paid == pytest.approx(0.39, abs=1e-6)
    results = [
        _result(prompt_id="paid", model_used=probe.PAID_MODEL, cost_usd=paid),
        _result(prompt_id="free", model_used=probe.FREE_MODEL, cost_usd=0.0),
    ]
    summary = probe.summarize(results)
    assert summary["spend"]["usd"] == pytest.approx(paid)
    assert summary["spend"]["paid_prompts"] == 1
    assert summary["models_used"] == sorted({probe.FREE_MODEL, probe.PAID_MODEL})


@pytest.mark.unit
def test_exhaustion_detection_triggers_the_paid_fallback() -> None:
    assert probe._is_exhausted('HTTP 429 {"error":{"message":"Provider returned error"}}')
    assert probe._is_exhausted("google/gemma-4-26b-a4b-it:free is temporarily rate-limited")
    assert not probe._is_exhausted("HTTP 400 invalid request")


@pytest.mark.unit
def test_transport_failure_becomes_a_recorded_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead call is a FAIL row with the error attached, not a missing row."""

    def boom(*args: object, **kwargs: object):
        raise urllib.error.HTTPError("u", 500, "server error", {}, None)

    monkeypatch.setattr(probe, "chat_once", boom)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    result = probe.probe_case(CALL_CASE, probe.real_specs())
    assert result.well_formed is False
    assert result.emitted is False
    assert result.errors and "500" in result.errors[0]
    summary = probe.summarize([result])
    assert summary["n_prompts"] == 1
    assert summary["verdict"] == "BLOCKED"
    assert summary["n_silent_non_calls"] == 0


@pytest.mark.unit
def test_probe_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        probe.probe_case(CALL_CASE, probe.real_specs())


# --------------------------------------------------------------------------- #
# The 007 mapping was checked first: well-formedness only, never quality
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_no_quality_or_groundedness_metric_leaks_into_the_probe() -> None:
    """Scanned over CODE, not prose: the docstring may name what it excludes."""
    import ast

    tree = ast.parse(pathlib.Path(probe.__file__).read_text())
    code_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            code_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            code_names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            code_names.add(node.name)
    banned = {
        "score_faithfulness",
        "groundedness_lexical_proxy",
        "compute_recall",
        "compute_mrr",
        "compute_ndcg",
        "answer_em",
        "answer_f1",
    }
    assert not (code_names & banned), code_names & banned
    # The harness's grounding proxy is OURS-ONLY and is not used here.
    from evals import leaderboard as lb

    assert lb.MAPPING["groundedness_lexical_proxy"] == lb.OURS_ONLY
    assert not hasattr(probe, "groundedness")


@pytest.mark.unit
def test_prompt_set_covers_every_real_tool_and_the_no_call_path() -> None:
    assert 5 <= len(probe.PROBE_PROMPTS) <= 10
    called = {case.expected_tool for case in probe.PROBE_PROMPTS}
    assert called == {"calculator", "knowledge_search", "knowledge_read", None}
    assert sum(1 for case in probe.PROBE_PROMPTS if case.expected_tool is None) >= 2
    # Real schema bounds are respected by the prompts themselves.
    for case in probe.PROBE_PROMPTS:
        assert len(case.user) <= 500