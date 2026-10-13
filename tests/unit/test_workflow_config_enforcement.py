"""T-V4-005 — config-validation enforcement-point proof (B-slice R2 vehicle).

DETERMINATION (ticket is determine-first): the enforcement vehicle **already
exists**.  The single point where every B-R1-fixed validator runs on every
import is ``LangflowAdapter.import_definition``'s security pass: for each node
it calls ``LangflowAdapter._check_config_values``
(``packages/workflow_engine/adapters/langflow_adapter.py``, wired in the node
security pass), which runs every allowlisted config value through
``LangflowAdapter._validate_config_value``.  Any rejection raises
``LangflowImportError`` *before* ``_build_node_spec`` constructs ``NodeSpec``,
so an invalid value never reaches ``NodeSpec.config`` or the revision hash.
``import_revision`` delegates to ``import_definition`` — there is no second
entry point and no other path from untrusted flow JSON into config.

This module therefore **verifies** (verification-only ticket): one rejection
per validator through that point, ``bypass = 0`` via direct lower-level calls,
a rule-by-rule sweep at the point, positive controls (no over-rejection), and
the B-R1-4 documented-drop alignment.  No product file is changed.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from workflow_engine import (
    LangflowAdapter,
    LangflowComponentSpec,
    LangflowImportError,
    NodeType,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers (self-contained: `tests` is not on the import path)
# ---------------------------------------------------------------------------


def _lf_node(
    node_id: str,
    component: str,
    *,
    template: dict[str, Any] | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Build a Langflow-1.x-shaped node object."""
    return {
        "id": node_id,
        "type": component,
        "data": {
            "type": component,
            "id": f"{component}-{node_id}",
            "node": {
                "display_name": display_name or component,
                "description": "Enforcement fixture",
                "template": template or {},
            },
        },
        "position": {"x": 0.0, "y": 0.0},
    }


def _lf_edge(
    edge_id: str,
    source: str,
    source_handle: str,
    target: str,
    target_handle: str,
) -> dict[str, Any]:
    """Build a Langflow-1.x-shaped edge object with resolved handle metadata."""
    return {
        "id": edge_id,
        "source": source,
        "target": target,
        "sourceHandle": f"{source}|{source_handle}|{source_handle}",
        "targetHandle": f"{target}|{target_handle}|{target_handle}",
        "data": {
            "sourceHandle": {
                "type": "output",
                "id": source_handle,
                "name": source_handle,
                "output_types": ["Message"],
                "selected_output": source_handle,
            },
            "targetHandle": {
                "type": "input",
                "id": target_handle,
                "name": target_handle,
                "fieldType": "str",
                "input_types": ["Message"],
            },
        },
    }


def _flow(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    name: str = "Enforcement Flow",
) -> dict[str, Any]:
    """Build a Langflow-1.x-shaped flow definition."""
    return {
        "id": "flow-enforcement",
        "name": name,
        "description": "",
        "data": {"nodes": nodes, "edges": edges},
    }


def _llm_template(**extra: Any) -> dict[str, Any]:
    template: dict[str, Any] = {"input_value": {"value": "hi"}}
    template.update(extra)
    return template


def _import_llm(template: dict[str, Any]):
    """Import a single ChatOpenAI node — the LLM config surface."""
    nodes = [_lf_node("lf-llm", "ChatOpenAI", template=template)]
    return LangflowAdapter.import_definition(
        _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
    )


def _max_rejection_size(exc: LangflowImportError) -> int:
    """Largest string length across every rejection value (payload bound)."""
    return max(
        len(str(value))
        for rejection in exc.rejections
        for value in rejection.values()
        if isinstance(value, str)
    )


def _import_component(component: str, template: dict[str, Any]):
    """Import a single node of *component*."""
    nodes = [_lf_node("lf-node", component, template=template)]
    return LangflowAdapter.import_definition(
        _flow(nodes, []), workflow_id="wf-1", workspace_id="ws-1"
    )


# Hostile samples per product config key (component, template field, sample).
# These cover every kind declared in ``_CONFIG_RULES``: string/allowlist,
# number, integer, boolean, identifier-string, list and dict.
_HOSTILE_SAMPLES: dict[str, tuple[str, str, Any]] = {
    "model": ("ChatOpenAI", "model_name", {"value": "definitely-not-vetted"}),
    "temperature": ("ChatOpenAI", "temperature", {"value": float("nan")}),
    "max_tokens": ("ChatOpenAI", "max_tokens", {"value": 10 ** 100_000}),
    "top_k": ("VectorRetriever", "top_k", {"value": 0}),
    "collection": ("VectorRetriever", "collection", {"value": "../../etc/passwd"}),
    "stream": ("ChatOpenAI", "stream", {"value": "yes"}),
    "tool": ("Tool", "tool_name", {"value": "a b;rm -rf"}),
    "instructions": ("ToolCallingAgent", "agent_instructions", {"value": "x" * 5000}),
    "routes": ("ConditionalRouter", "conditions", {"value": ["x" * 10_000_000]}),
}


# ---------------------------------------------------------------------------
# 1. Determination — the vehicle exists and is a single point
# ---------------------------------------------------------------------------


class TestEnforcementPointExists:
    """The single point: import_definition's security pass (_check_config_values)."""

    def test_hostile_config_is_rejected_at_the_point(self) -> None:
        with pytest.raises(LangflowImportError) as exc_info:
            _import_llm(_llm_template(temperature={"value": float("nan")}))
        codes = {r["code"] for r in exc_info.value.rejections}
        assert "INVALID_CONFIG_VALUE" in codes

    def test_point_yields_no_partial_result(self) -> None:
        """Fail-closed: a rejection aborts the import, no partial definition."""
        with pytest.raises(LangflowImportError):
            _import_llm(_llm_template(max_tokens={"value": 10 ** 100_000}))

    def test_point_runs_before_node_spec_construction(self) -> None:
        """The rejection carries the config code, not a later edge/node code:
        config validation is wired into the node security pass that precedes
        ``_build_node_spec``, so invalid config never becomes a NodeSpec."""
        with pytest.raises(LangflowImportError) as exc_info:
            _import_llm(_llm_template(model_name={"value": "rogue-model"}))
        codes = {r["code"] for r in exc_info.value.rejections}
        assert codes == {"INVALID_CONFIG_VALUE"}

    def test_alternate_public_entry_delegates_to_the_point(self) -> None:
        """import_revision is the only other public entry and it delegates:
        no second (unenforced) path into config."""
        hostile_flow = _flow(
            [
                _lf_node(
                    "lf-llm",
                    "ChatOpenAI",
                    template=_llm_template(temperature={"value": float("nan")}),
                )
            ],
            [],
        )
        with pytest.raises(LangflowImportError) as exc_info:
            LangflowAdapter.import_revision(
                hostile_flow, workflow_id="wf-1", workspace_id="ws-1"
            )
        assert "INVALID_CONFIG_VALUE" in {r["code"] for r in exc_info.value.rejections}


# ---------------------------------------------------------------------------
# 2. 4/4 B-R1 validators enforced at the point (one rejection per validator)
# ---------------------------------------------------------------------------


class TestFourValidatorsEnforcedAtThePoint:
    """B-R1-1..4 — each proven through import_definition, the single point."""

    def test_br1_1_huge_integer_never_crashes_rejects_bounded(self) -> None:
        """B-R1-1: 100k-digit int → structured rejection, no raw ValueError."""
        with pytest.raises(LangflowImportError) as exc_info:
            _import_llm(_llm_template(max_tokens={"value": 10 ** 100_000}))
        assert "INVALID_CONFIG_VALUE" in {r["code"] for r in exc_info.value.rejections}
        assert _max_rejection_size(exc_info.value) <= 300
        message = " ".join(r["message"] for r in exc_info.value.rejections)
        assert re.search(r"\d{33,}", message) is None  # echo is digit-capped
        assert "digits" in message

    def test_br1_2_non_finite_never_reaches_config(self) -> None:
        """B-R1-2: NaN / ±inf rejected before any range comparison."""
        for hostile in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(LangflowImportError) as exc_info:
                _import_llm(_llm_template(temperature={"value": hostile}))
            assert "INVALID_CONFIG_VALUE" in {
                r["code"] for r in exc_info.value.rejections
            }
            message = " ".join(r["message"] for r in exc_info.value.rejections)
            assert "finite" in message

    def test_br1_3_list_elements_validated_with_index(self) -> None:
        """B-R1-3: routes elements must be bounded strings; index is reported."""
        for hostile in (["x" * 10_000_000], [float("nan")], ["ok", 42]):
            with pytest.raises(LangflowImportError) as exc_info:
                _import_component("ConditionalRouter", {"conditions": hostile})
            message = " ".join(r["message"] for r in exc_info.value.rejections)
            assert "INVALID_CONFIG_VALUE" in {
                r["code"] for r in exc_info.value.rejections
            }
            assert "routes" in message

    def test_br1_4_dict_drop_is_the_documented_behavior_at_the_point(self) -> None:
        """B-R1-4: dict-valued tool_metadata is dropped from templates (chosen
        branch: document-and-test).  The import succeeds hash-neutrally and
        config never carries the object payload."""
        with_metadata = _flow(
            [
                _lf_node(
                    "lf-tool",
                    "Tool",
                    template={
                        "tool_name": {"value": "approved_tool"},
                        "tool_metadata": {"a": 1, "b": [1, 2]},
                    },
                )
            ],
            [],
        )
        without_metadata = _flow(
            [_lf_node("lf-tool", "Tool", template={"tool_name": {"value": "approved_tool"}})],
            [],
        )
        kept = LangflowAdapter.import_revision(
            with_metadata, workflow_id="wf-1", workspace_id="ws-1"
        )
        bare = LangflowAdapter.import_revision(
            without_metadata, workflow_id="wf-1", workspace_id="ws-1"
        )
        assert "tool_metadata" not in kept.spec.nodes[0].config
        assert kept.content_hash == bare.content_hash  # drop is hash-neutral


# ---------------------------------------------------------------------------
# 3. Bypass = 0 — the lower-level paths reject on their own
# ---------------------------------------------------------------------------


class TestBypassIsZero:
    """Calling the point's machinery directly still rejects — no bypass path."""

    @pytest.mark.parametrize(
        ("config_key", "hostile"),
        [
            ("model", "rogue-model"),
            ("temperature", float("nan")),
            pytest.param("max_tokens", 10 ** 100_000, id="max_tokens-huge-int"),
            ("top_k", 0),
            ("collection", "../../etc/passwd"),
            ("stream", "yes"),
            ("tool", "a b;rm -rf"),
            ("instructions", "x" * 5000),
            ("routes", ["x" * 10_000_000]),
            ("tool_metadata", "not-a-dict"),
        ],
    )
    def test_direct_validate_config_value_rejects(self, config_key: str, hostile: Any) -> None:
        reason = LangflowAdapter._validate_config_value(config_key, hostile)
        assert reason is not None, f"{config_key} must reject {hostile!r} at the validator"
        assert len(reason) <= 300  # reason itself stays bounded

    @pytest.mark.parametrize(
        ("config_key", "valid"),
        [
            ("model", "gpt-4o-mini"),
            ("temperature", 0.2),
            ("max_tokens", 512),
            ("top_k", 5),
            ("collection", "docs_v1.2-3"),
            ("stream", True),
            ("tool", "approved_tool"),
            ("instructions", "Be strict."),
            ("routes", ["true", "false"]),
            ("tool_metadata", {"a": 1}),  # declared rule: object accepted
        ],
    )
    def test_direct_validate_config_value_accepts(self, config_key: str, valid: Any) -> None:
        assert LangflowAdapter._validate_config_value(config_key, valid) is None

    def test_direct_check_config_values_rejects_hostile_node(self) -> None:
        """The per-node checker used by the security pass rejects directly."""
        spec = LangflowAdapter.catalog()["chatopenai"]
        raw_node = _lf_node(
            "lf-llm",
            "ChatOpenAI",
            template=_llm_template(max_tokens={"value": 10 ** 100_000}),
        )
        rejections = LangflowAdapter._check_config_values(raw_node, "lf-llm", spec)
        assert rejections
        assert all(r["code"] == "INVALID_CONFIG_VALUE" for r in rejections)
        assert rejections[0]["node_id"] == "lf-llm"

    def test_direct_check_config_values_accepts_clean_node(self) -> None:
        spec = LangflowAdapter.catalog()["chatopenai"]
        raw_node = _lf_node(
            "lf-llm",
            "ChatOpenAI",
            template=_llm_template(model_name={"value": "gpt-4o-mini"}, temperature={"value": 0.2}),
        )
        assert LangflowAdapter._check_config_values(raw_node, "lf-llm", spec) == []

    def test_format_number_never_raises_on_hostile_values(self) -> None:
        """B-R1-1 machinery: bounded echo helper never converts the value."""
        from workflow_engine.adapters.langflow_adapter import _format_number

        huge = 10 ** 100_000
        hostile_values = (huge, -huge, float("nan"), float("inf"), float("-inf"), 1.5, 7)
        for hostile in hostile_values:
            rendered = _format_number(hostile)
            assert isinstance(rendered, str)
            assert len(rendered) <= 300


# ---------------------------------------------------------------------------
# 4. Rule-by-rule sweep at the point — no config key escapes the point
# ---------------------------------------------------------------------------


class TestEveryRuleEnforcedAtThePoint:
    """Every declared rule is enforced through import_definition, not just LLM fields."""

    @pytest.mark.parametrize("config_key", sorted(_HOSTILE_SAMPLES))
    def test_hostile_sample_rejected_at_the_point(self, config_key: str) -> None:
        component, field_name, sample = _HOSTILE_SAMPLES[config_key]
        template = {field_name: sample}
        if component == "ChatOpenAI":
            template["input_value"] = {"value": "hi"}
        with pytest.raises(LangflowImportError) as exc_info:
            _import_component(component, template)
        assert "INVALID_CONFIG_VALUE" in {
            r["code"] for r in exc_info.value.rejections
        }, f"{config_key} escaped the enforcement point"

    def test_valid_samples_accepted_at_the_point(self) -> None:
        """Positive control: the same components import clean with valid values."""
        llm = _import_component(
            "ChatOpenAI",
            {
                "input_value": {"value": "hi"},
                "model_name": {"value": "gpt-4o-mini"},
                "temperature": {"value": 0.2},
                "max_tokens": {"value": 512},
                "stream": {"value": False},
            },
        )
        node = llm.nodes[0]
        assert node.config["model"] == "gpt-4o-mini"
        assert node.config["temperature"] == 0.2
        assert node.config["max_tokens"] == 512
        assert node.config["stream"] is False

        retriever = _import_component(
            "VectorRetriever",
            {"query": {"value": "hello"}, "top_k": {"value": 5}, "collection": {"value": "docs"}},
        )
        assert retriever.nodes[0].config["top_k"] == 5
        assert retriever.nodes[0].config["collection"] == "docs"

        router = _import_component(
            "ConditionalRouter", {"conditions": {"value": ["true", "false"]}}
        )
        assert router.nodes[0].config["routes"] == ["true", "false"]

        tool = _import_component("Tool", {"tool_name": {"value": "approved_tool"}})
        assert tool.nodes[0].config["tool"] == "approved_tool"


# ---------------------------------------------------------------------------
# 5. Regression controls — the point does not over-reject
# ---------------------------------------------------------------------------


class TestNoOverRejection:
    """The vehicle must not have become stricter than the B-slice accepts."""

    def test_vetted_full_flow_still_imports(self) -> None:
        nodes = [
            _lf_node("lf-trigger", "ChatInput"),
            _lf_node(
                "lf-llm",
                "ChatOpenAI",
                template={"input_value": "hi", "model_name": {"value": "gpt-4o-mini"}},
            ),
            _lf_node("lf-out", "ChatOutput"),
        ]
        edges = [
            _lf_edge("e1", "lf-trigger", "message", "lf-llm", "input_value"),
            _lf_edge("e2", "lf-llm", "message", "lf-out", "input_value"),
        ]
        definition = LangflowAdapter.import_definition(
            _flow(nodes, edges), workflow_id="wf-1", workspace_id="ws-1"
        )
        assert [n.id for n in definition.nodes] == ["lf-trigger", "lf-llm", "lf-out"]
        assert definition.nodes[1].config["model"] == "gpt-4o-mini"

    def test_boundary_values_accepted_at_the_point(self) -> None:
        _import_component(
            "ChatOpenAI",
            {"input_value": "hi", "temperature": {"value": 0.0}, "max_tokens": {"value": 1}},
        )
        _import_component(
            "ChatOpenAI",
            {"input_value": "hi", "temperature": {"value": 2.0}, "max_tokens": {"value": 32768}},
        )

    def test_catalog_specs_are_the_only_config_source(self) -> None:
        """A-1 holds at the point: non-allowlisted template fields are ignored,
        so enforcement follows the catalog, not the flow's field set."""
        spec: LangflowComponentSpec = LangflowAdapter.catalog()["chatopenai"]
        assert set(spec.config_fields.values()) == {"model", "temperature", "max_tokens", "stream"}
        assert all(t is not NodeType.TOOL for t in (spec.node_type,))
