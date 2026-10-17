"""T-EVAL-006 — covering tests for the BFCL-style battery (offline, no network).

Two ticket metrics are structural here, not asserted by hand:

* **unparseable-as-non-call: 0** — every model output lands in exactly one
  closed bucket; the bucket set is fixed and the counts must sum to n.
* **official-shaped numbers mislabeled: 0** — every score carries
  ``OURS-PENDING-OFFICIAL`` and the official ``bfcl_ast_accuracy`` column renders
  ``pending`` through the 007 shaper, because the official script is not
  vendored.

Run from the repo root:
    /root/ko-venv/bin/pytest evals/test_bfcl_battery.py -q
"""

from __future__ import annotations

import ast
import dataclasses
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

from evals import bfcl_astspec as astspec  # noqa: E402
from evals import bfcl_battery as battery  # noqa: E402
from evals import leaderboard as lb  # noqa: E402

CALL_ITEM = battery.BATTERY[0]
SEARCH_ITEM = next(i for i in battery.BATTERY if i.item_id == "simple-search-topk")
NO_CALL_ITEM = next(i for i in battery.BATTERY if i.category == "irrelevance")


def _native(name: str, arguments: object) -> dict[str, object]:
    return {"content": None, "tool_calls": [{"function": {"name": name, "arguments": json.dumps(arguments)}}]}


# --------------------------------------------------------------------------- #
# AST-match spec (ours, pending official)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_ast_equality_rules() -> None:
    eq = astspec.ast_equal
    assert eq({"a": 1}, {"a": 1})
    # Rule 2: int/float are one numeric kind.
    assert eq(2, 2.0)
    # Rule 3: a number's string form matches (models return numbers as strings).
    assert eq("3", 3)
    assert eq(3, "3")
    assert not eq("3.5", 3)
    # Rule 4: booleans never degrade into numbers.
    assert not eq(True, 1)
    assert not eq(False, 0)
    assert eq(True, "true")
    assert eq(False, "False")
    # Rule 5: null-ish strings.
    assert eq(None, "null")
    assert eq(None, "")
    assert not eq(None, "none-ish")
    # Rule 6: lists are multisets by default, ordered when asked.
    assert eq([1, 2, 3], [3, 1, 2])
    assert not eq([1, 2, 3], [3, 2], strict_list=True)
    assert astspec.ast_equal([1, 2], [2, 1], strict_list=True) is False
    assert astspec.ast_equal([1, 2], [1, 2], strict_list=True) is True
    assert not eq([1, 2], [1, 2, 2])
    # Rule 7: dicts need identical key sets.
    assert eq({"a": {"b": [1, 2]}}, {"a": {"b": [2, 1]}})
    assert not eq({"a": 1}, {"a": 1, "b": 2})
    assert not eq({"a": 1}, {"b": 1})


@pytest.mark.unit
def test_ast_match_is_one_to_one_and_order_insensitive() -> None:
    gold = ({"name": "calculator", "arguments": {"expression": "1+1"}},)
    assert astspec.ast_match(({"name": "calculator", "arguments": {"expression": "1+1"}},), gold)
    # Extra prediction on the wrong tool does not break a correct gold match...
    assert astspec.ast_match(
        (
            {"name": "calculator", "arguments": {"expression": "1+1"}},
            {"name": "knowledge_search", "arguments": {"query": "x"}},
        ),
        gold,
    )
    # ...but one prediction cannot satisfy two gold calls.
    two = gold * 2
    assert not astspec.ast_match(({"name": "calculator", "arguments": {"expression": "1+1"}},), two)
    assert astspec.ast_match(
        (
            {"name": "calculator", "arguments": {"expression": "1+1"}},
            {"name": "calculator", "arguments": {"expression": "1+1"}},
        ),
        two,
    )


@pytest.mark.unit
def test_ast_diff_explains_the_mismatch() -> None:
    gold = ({"name": "knowledge_search", "arguments": {"query": "sla", "top_k": 3}},)
    pred = ({"name": "knowledge_search", "arguments": {"query": "other", "top_k": 5}},)
    reasons = astspec.ast_diff(gold, pred)
    assert any("query" in reason for reason in reasons)
    assert any("top_k" in reason for reason in reasons)
    # A missing argument is named as missing, not as a value diff.
    assert any(
        "missing argument 'top_k'" in reason
        for reason in astspec.ast_diff(gold, ({"name": "knowledge_search", "arguments": {"query": "sla"}},))
    )
    # A wrong tool is counted by name.
    assert any("calculator" in reason for reason in astspec.ast_diff(gold, ({"name": "calculator", "arguments": {}},)))


@pytest.mark.unit
def test_contract_shape_matches_the_007_placeholder() -> None:
    """Swapping the vendored official scorer in must need no redesign."""
    result = astspec.bfcl_ast_accuracy(
        [
            astspec.AstComparison(
                pred_calls=({"name": "calculator", "arguments": {"expression": "1+1"}},),
                gold_calls=({"name": "calculator", "arguments": {"expression": "1+1"}},),
            ),
            astspec.AstComparison(pred_calls=(), gold_calls=({"name": "calculator", "arguments": {}},)),
        ]
    )
    assert tuple(result) == astspec.CONTRACT_KEYS == ("total", "correct", "ast_accuracy")
    assert result == {"total": 2, "correct": 1, "ast_accuracy": 0.5}
    assert astspec.bfcl_ast_accuracy([]) == {"total": 0, "correct": 0, "ast_accuracy": 0.0}
    assert astspec.SWAP_TARGET == "evals/vendor/bfcl_eval.py"
    assert astspec.SWAP_REGISTRY_ENTRY == "bfcl_ast_metrics"


@pytest.mark.unit
def test_official_placeholder_is_still_standing() -> None:
    """The label is only correct while the official script is not vendored."""
    from evals.vendor import VENDOR_PROVENANCE, vendored_source_for
    from evals.vendor.placeholders import bfcl_ast_accuracy as official_placeholder

    assert lb.official_pending("bfcl_ast_accuracy") is True
    assert vendored_source_for("bfcl_ast_accuracy").status == "placeholder"
    source = vendored_source_for("bfcl_ast_accuracy")
    assert source.name == astspec.SWAP_REGISTRY_ENTRY
    assert any(s.name == "bfcl_ast_metrics" and s.status == "placeholder" for s in VENDOR_PROVENANCE)
    with pytest.raises(lb.OfficialMetricNotVendored):
        official_placeholder()


# --------------------------------------------------------------------------- #
# Buckets: exactly one per output, nothing dropped
# --------------------------------------------------------------------------- #


def _result(bucket: str, **kwargs) -> battery.ItemResult:
    defaults = dict(
        item_id="x",
        category="simple",
        model_used=lb.VENDOR_PROVENANCE[0].name and "google/gemma-4-26b-a4b-it:free",
        bucket=bucket,
        emitted=bucket in ("ast_match", "ast_mismatch", "invalid_arguments", "wrong_function", "false_call"),
        mode="native",
        n_predicted_calls=1,
        n_gold_calls=1,
        ast_match=bucket == "ast_match",
        errors=() if bucket == "ast_match" else ("reason",),
        predicted_calls=({"name": "calculator", "arguments": {"expression": "1+1"}},),
        gold_calls=CALL_ITEM.gold_calls,
    )
    defaults.update(kwargs)
    return battery.ItemResult(**defaults)


@pytest.mark.unit
def test_every_output_lands_in_exactly_one_closed_bucket() -> None:
    """unparseable-as-non-call: 0 — the buckets partition the outputs."""
    results = [_result(bucket) for bucket in battery.BUCKETS]
    counts = battery.bucket_counts(results)
    assert sum(counts.values()) == len(results)
    assert set(counts) == set(battery.BUCKETS)
    summary = battery.summarize(results)
    assert summary["unparseable_as_non_call"] == 0
    assert summary["n_items"] == len(results)
    # Each row carries its own gold calls: the summary reaches no global.
    assert all(r.gold_calls == CALL_ITEM.gold_calls for r in results)
    # Any bucket value outside the closed set is impossible by construction.
    for result in results:
        assert result.bucket in battery.BUCKETS


@pytest.mark.unit
def test_classify_fills_every_bucket_from_a_real_message() -> None:
    specs = battery.real_specs()
    names = set(specs)
    # ast_match
    ok = battery.classify(SEARCH_ITEM, _native("knowledge_search", {"query": "SLA penalty clause", "top_k": 3}), names, specs)
    assert ok.bucket == "ast_match" and ok.errors == ()
    # ast_mismatch: right call, wrong values
    wrong = battery.classify(SEARCH_ITEM, _native("knowledge_search", {"query": "SLA penalty clause", "top_k": 9}), names, specs)
    assert wrong.bucket == "ast_mismatch" and wrong.errors
    # invalid_arguments: schema-valid call, wrong type (top_k as string)
    typed = battery.classify(SEARCH_ITEM, _native("knowledge_search", {"query": "SLA penalty clause", "top_k": "3"}), names, specs)
    assert typed.bucket == "invalid_arguments"
    # wrong_function: a real tool, but not the gold one
    tool = battery.classify(CALL_ITEM, _native("knowledge_search", {"query": "x"}), names, specs)
    assert tool.bucket == "wrong_function"
    # no_call: prose instead of a call
    silent = battery.classify(CALL_ITEM, {"content": "I will call the calculator tool."}, names, specs)
    assert silent.bucket == "no_call" and silent.errors
    # correct_abstain / false_call on an irrelevance item
    abstained = battery.classify(NO_CALL_ITEM, {"content": "Water boils at 100 C."}, names, specs)
    assert abstained.bucket == "correct_abstain" and abstained.scored_correct
    false_call = battery.classify(NO_CALL_ITEM, _native("calculator", {"expression": "1+1"}), names, specs)
    assert false_call.bucket == "false_call" and not false_call.scored_correct
    # Multi-call item where only one call is emitted.
    multi = next(i for i in battery.BATTERY if len(i.gold_calls) == 2)
    partial = battery.classify(multi, _native("calculator", {"expression": "6 * 7"}), names, specs)
    assert partial.bucket == "ast_mismatch"
    assert any("knowledge_search" in reason for reason in partial.errors)


@pytest.mark.unit
def test_transport_failure_is_a_bucket_not_a_missing_row(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object):
        raise urllib.error.HTTPError("u", 500, "server error", {}, None)

    monkeypatch.setattr(battery, "chat_once", boom)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    result = battery.run_item(CALL_ITEM, specs=battery.real_specs(), functions=battery.bfcl_functions())
    assert result.bucket == "transport_error"
    assert result.errors and "500" in result.errors[0]
    assert not result.scored_correct
    summary = battery.summarize([result])
    assert summary["unparseable_as_non_call"] == 0
    assert summary["buckets"]["transport_error"] == 1


# --------------------------------------------------------------------------- #
# No official-shaped number is mislabelled
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_leaderboard_keeps_the_official_column_pending() -> None:
    summary = battery.summarize([_result("ast_match"), _result("ast_mismatch", ast_match=False)])
    table = battery.leaderboard_rows(summary)
    header, row = table[0], table[1]
    assert header[2] == "bfcl_ast_accuracy"
    assert row[2] == lb.PENDING  # not a number of ours
    assert header[3] == f"ast_accuracy_{astspec.OURS_PENDING_OFFICIAL}"
    for column in header[3:]:
        assert column.endswith(astspec.OURS_PENDING_OFFICIAL)
    assert summary["label"] == astspec.OURS_PENDING_OFFICIAL
    # The ours side is structurally separate from the official side.
    assert set(header[3:]).isdisjoint(set(header[2:3]))


@pytest.mark.unit
def test_scores_are_labelled_in_code_not_just_in_prose() -> None:
    """AST-scan the battery + spec modules: the label and the pending gate."""
    code_names: set[str] = set()
    for module in (battery, astspec):
        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                code_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                code_names.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                code_names.add(node.value)
    assert astspec.OURS_PENDING_OFFICIAL in code_names
    # No official-metric symbol is imported into the battery: the official
    # number can only come from the 007 shaper, which renders it pending.
    assert "bfcl_ast_accuracy" in code_names  # our contract function, clearly ours
    source = pathlib.Path(battery.__file__).read_text()
    assert "vendor.bfcl" not in source
    assert "ours_ast_accuracy" in source


@pytest.mark.unit
def test_ast_accuracy_is_reported_over_call_items_only() -> None:
    results = [
        _result(
            "ast_match",
            item_id="a",
            category="simple",
            predicted_calls=CALL_ITEM.gold_calls,
        ),
        _result(
            "ast_mismatch",
            item_id="b",
            category="multiple",
            ast_match=False,
            n_gold_calls=2,
            gold_calls=next(i for i in battery.BATTERY if len(i.gold_calls) == 2).gold_calls,
            predicted_calls=(next(i for i in battery.BATTERY if len(i.gold_calls) == 2).gold_calls[:1]),
        ),
        _result("correct_abstain", item_id="c", category="irrelevance", n_gold_calls=0,
                n_predicted_calls=0, emitted=False, ast_match=False, errors=(), predicted_calls=()),
    ]
    summary = battery.summarize(results)
    assert summary["n_items"] == 3
    assert summary["n_call_items"] == 2
    assert summary["n_irrelevance_items"] == 1
    assert summary["ours_ast_accuracy"] == {"total": 2, "correct": 1, "ast_accuracy": 0.5}
    assert summary["by_category"]["irrelevance"] == {"n": 1, "n_correct": 1}


# --------------------------------------------------------------------------- #
# Battery + meter wiring
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_battery_functions_are_the_real_v3_schemas() -> None:
    functions = {f["name"]: f for f in battery.bfcl_functions()}
    assert set(functions) == {"calculator", "knowledge_search", "knowledge_read"}
    assert functions["knowledge_search"]["parameters"]["additionalProperties"] is False
    assert functions["knowledge_search"]["parameters"]["properties"]["top_k"]["type"] == "integer"
    assert functions["calculator"]["description"]
    # BFCL declaration shape.
    for function in functions.values():
        assert set(function) == {"name", "description", "parameters"}


@pytest.mark.unit
def test_battery_covers_the_categories_and_the_real_tools() -> None:
    assert 8 <= len(battery.BATTERY) <= 15
    categories = {item.category for item in battery.BATTERY}
    assert categories == {"simple", "multiple", "parallel", "irrelevance"}
    assert all(len(item.gold_calls) >= 1 for item in battery.BATTERY if item.category != "irrelevance")
    assert all(item.gold_calls == () for item in battery.BATTERY if item.category == "irrelevance")
    # Every gold call names a real V3 tool and its gold arguments validate.
    specs = battery.real_specs()
    for item in battery.BATTERY:
        for call in item.gold_calls:
            assert call["name"] in specs
            assert battery.validate_arguments(specs[call["name"]], call["arguments"]) == []
    assert sum(1 for i in battery.BATTERY if i.category == "multiple") >= 2


@pytest.mark.unit
def test_pricing_is_reused_from_the_009_harness() -> None:
    assert battery.price_usd(battery.FREE_MODEL, 1_000_000, 1_000_000) == 0.0
    assert battery.price_usd(battery.PAID_MODEL, 1_000_000, 1_000_000) == pytest.approx(0.39, abs=1e-6)
    assert battery.FREE_MODEL == "google/gemma-4-26b-a4b-it:free"
    assert battery.PAID_MODEL == "google/gemma-4-26b-a4b-it"
    assert battery._looks_exhausted("HTTP 429 provider returned error")


@pytest.mark.unit
def test_rows_render_every_item() -> None:
    results = [
        _result("ast_match", item_id=f"item-{index}")
        for index in range(len(battery.BUCKETS))
    ]
    table = battery.render_rows(results)
    body = [line for line in table.splitlines()[2:] if line.strip()]
    assert len(body) == len(results)
    for result in results:
        assert result.item_id in table


@pytest.mark.unit
def test_no_product_code_is_touched() -> None:
    """The battery writes nothing outside evals/ and imports no product tool."""
    source = pathlib.Path(battery.__file__).read_text()
    for banned in ("CalculatorTool(", "KnowledgeSearchTool(", "qdrant", "upsert("):
        assert banned not in source
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__",
        "argparse",
        "ast",
        "dataclasses",
        "json",
        "os",
        "pathlib",
        "sys",
        "urllib",
        "collections",
        "typing",
        "pytest",
        "evals",
    }


@pytest.mark.unit
def test_summarize_is_json_serialisable() -> None:
    summary = battery.summarize([_result("ast_match")])
    assert json.loads(json.dumps(summary))["label"] == astspec.OURS_PENDING_OFFICIAL
    assert dataclasses.asdict(_result("ast_match"))["bucket"] == "ast_match"
