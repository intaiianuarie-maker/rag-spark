"""T-EVAL-006 — BFCL-style tool-use battery on gemma (**AST exact match**).

This is the battery T-EVAL-009 unblocked: gemma emitted well-formed calls on
the real V3 schemas, so now we score them.

Two honesty rules govern every number printed here
--------------------------------------------------
1. **The official BFCL script is NOT vendored.** The T-EVAL-007 placeholder
   (``evals/vendor/placeholders.py::bfcl_ast_metrics``) still stands, so every
   score in this file is labelled **OURS-PENDING-OFFICIAL** and the official
   ``bfcl_ast_accuracy`` column renders ``pending`` through the 007 table shaper
   — never a number of ours wearing an official label. The comparison itself
   implements the BFCL paper's AST-match spec (see ``evals/bfcl_astspec.py``,
   which also documents the one-module swap that retires the label).
2. **GEMMA-ONLY** per the wave's user setup: ``google/gemma-4-26b-a4b-it:free``
   first, automatic paid fallback on exhaustion, every call metered and the
   spend printed. (T-EVAL-009's first paid spend: $0.000939.)

Battery design, stated up front so the numbers are readable honestly
--------------------------------------------------------------------
The functions are the REAL V3 ones — ``calculator``, ``knowledge_search``,
``knowledge_read`` — taken from ``packages/agents/tools.py`` via the 009
harness's ``real_specs()`` and rendered in BFCL's function-declaration shape.
No invented function, no invented schema.

AST-exact-match compares the argument *values*, so a battery item whose gold
query string the model is expected to paraphrase measures paraphrase luck, not
tool-use skill. Every item therefore quotes the exact argument values verbatim
in the prompt (e.g. ``query "SLA penalty clause"``), which makes a pass mean
"the model copied and typed the arguments correctly" — the thing a battery
should isolate. That is stated, not hidden; loosening it to free-form
paraphrase is a different (harder) metric and is not claimed here.

Every model output lands in EXACTLY ONE scored bucket
----------------------------------------------------
``ast_match`` | ``invalid_arguments`` | ``ast_mismatch`` | ``wrong_function`` |
``no_call`` | ``correct_abstain`` | ``false_call`` | ``transport_error``.
The bucket is a closed set and the summary asserts the bucket counts sum to the
number of items, so an output cannot fall between buckets or be dropped —
"unparseable-as-non-call: 0".

Run (from repo root):
    /root/ko-venv/bin/python3 evals/bfcl_battery.py
    /root/ko-venv/bin/python3 evals/bfcl_battery.py --no-paid-fallback
    /root/ko-venv/bin/pytest evals/test_bfcl_battery.py -q
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import sys
import urllib.error
from collections.abc import Sequence
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (REPO / "packages", REPO / "apps" / "api", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evals import leaderboard as lb  # noqa: E402
from evals.bfcl_astspec import (  # noqa: E402
    OURS_PENDING_OFFICIAL,
    AstComparison,
    ast_diff,
    ast_match,
    bfcl_ast_accuracy,
)
from evals.gemma_call_probe import (  # noqa: E402
    FREE_MODEL,
    PAID_MODEL,
    chat_once,
    extract_call,
    real_specs,
    validate_arguments,
)
from evals.gemma_call_probe import (
    _is_exhausted as _looks_exhausted,
)
from evals.gemma_call_probe import (
    _price_usd as price_usd,
)

# --------------------------------------------------------------------------- #
# The battery
# --------------------------------------------------------------------------- #

_UUID_A = "11111111-1111-4111-8111-111111111111"
_UUID_B = "22222222-2222-4222-8222-222222222222"
_UUID_C = "33333333-3333-4333-8333-333333333333"


@dataclasses.dataclass(frozen=True)
class BatteryItem:
    """One BFCL-style item: a prompt, its category and its gold call set."""

    item_id: str
    category: str  # simple | multiple | parallel | irrelevance
    user: str
    gold_calls: tuple[dict[str, Any], ...]


BATTERY: tuple[BatteryItem, ...] = (
    BatteryItem(
        "simple-calc-sum",
        "simple",
        'Use the calculator tool to evaluate the expression "45 * 12 + 10".',
        ({"name": "calculator", "arguments": {"expression": "45 * 12 + 10"}},),
    ),
    BatteryItem(
        "simple-calc-parens",
        "simple",
        'Use the calculator tool to evaluate the expression "(128 - 46) * 3".',
        ({"name": "calculator", "arguments": {"expression": "(128 - 46) * 3"}},),
    ),
    BatteryItem(
        "simple-search-query",
        "simple",
        'Use the knowledge search tool to search for the exact query '
        '"termination notice period".',
        ({"name": "knowledge_search", "arguments": {"query": "termination notice period"}},),
    ),
    BatteryItem(
        "simple-search-topk",
        "simple",
        'Use the knowledge search tool to search for the exact query '
        '"SLA penalty clause" and set top_k to 3.',
        (
            {
                "name": "knowledge_search",
                "arguments": {"query": "SLA penalty clause", "top_k": 3},
            },
        ),
    ),
    BatteryItem(
        "simple-read-triple",
        "simple",
        f"Use the knowledge read tool to read document id {_UUID_A}, "
        f"document version id {_UUID_B} and chunk id {_UUID_C}.",
        (
            {
                "name": "knowledge_read",
                "arguments": {
                    "document_id": _UUID_A,
                    "document_version_id": _UUID_B,
                    "chunk_id": _UUID_C,
                },
            },
        ),
    ),
    BatteryItem(
        "multiple-calc-then-search",
        "multiple",
        "Two steps, both tools: first use the calculator on the expression "
        '"6 * 7", then use the knowledge search tool on the exact query '
        '"onboarding checklist".',
        (
            {"name": "calculator", "arguments": {"expression": "6 * 7"}},
            {"name": "knowledge_search", "arguments": {"query": "onboarding checklist"}},
        ),
    ),
    BatteryItem(
        "multiple-search-then-calc",
        "multiple",
        "Two steps, both tools, in this order: first use the knowledge search "
        'tool on the exact query "retention policy", then use the calculator on '
        'the expression "365 - 30".',
        (
            {"name": "knowledge_search", "arguments": {"query": "retention policy"}},
            {"name": "calculator", "arguments": {"expression": "365 - 30"}},
        ),
    ),
    BatteryItem(
        "parallel-two-tools",
        "parallel",
        "These are independent, so do both: use the calculator on the "
        'expression "1024 / 8" and use the knowledge search tool on the exact '
        'query "incident postmortem".',
        (
            {"name": "calculator", "arguments": {"expression": "1024 / 8"}},
            {"name": "knowledge_search", "arguments": {"query": "incident postmortem"}},
        ),
    ),
    BatteryItem(
        "irrelevance-general-knowledge",
        "irrelevance",
        "In one short sentence, what is the boiling point of water at sea "
        "level? No tool is needed — answer from your own knowledge.",
        (),
    ),
    BatteryItem(
        "irrelevance-explicit-refusal",
        "irrelevance",
        "Do not call any tool. Reply with the single word: ready.",
        (),
    ),
    BatteryItem(
        "irrelevance-names-the-tool",
        "irrelevance",
        "Name the calculator tool in your answer, explain what it does in one "
        "sentence, and do not call it.",
        (),
    ),
)

#: The closed set of scored buckets. Every model output gets exactly one.
BUCKETS: tuple[str, ...] = (
    "ast_match",
    "invalid_arguments",
    "ast_mismatch",
    "wrong_function",
    "no_call",
    "correct_abstain",
    "false_call",
    "transport_error",
)


def bfcl_functions() -> list[dict[str, Any]]:
    """The real V3 functions in BFCL's declaration shape."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema,
        }
        for spec in real_specs().values()
    ]


# --------------------------------------------------------------------------- #
# One item
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class ItemResult:
    """One scored item. ``bucket`` is always exactly one of :data:`BUCKETS`."""

    item_id: str
    category: str
    model_used: str
    bucket: str
    emitted: bool
    mode: str
    n_predicted_calls: int
    n_gold_calls: int
    ast_match: bool
    errors: tuple[str, ...] = ()
    predicted_calls: tuple[dict[str, Any], ...] = ()
    #: The gold call set this row was scored against, so a result is
    #: self-contained (the summary never reaches back into a global).
    gold_calls: tuple[dict[str, Any], ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    fallback_reason: str = ""

    @property
    def scored_correct(self) -> bool:
        return self.bucket in ("ast_match", "correct_abstain")

    @property
    def is_call_item(self) -> bool:
        return self.n_gold_calls > 0


def classify(
    item: BatteryItem,
    message: dict[str, Any],
    tool_names: set[str],
    specs: dict[str, Any],
) -> ItemResult:
    """Put one model output into exactly one bucket, with a recorded reason.

    Precedence (deterministic, so a row can only ever mean one thing):
    ``transport`` -> ``no_call``/``correct_abstain`` -> ``wrong_function`` ->
    ``invalid_arguments`` (schema) -> ``ast_mismatch`` -> ``ast_match``.
    """
    extracted = extract_call(message, tool_names)
    native = message.get("tool_calls") or []
    calls: list[dict[str, Any]] = []
    for entry in native:
        function = entry.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        calls.append({"name": function.get("name"), "arguments": arguments})
    if not calls and extracted["emitted"]:
        calls = [{"name": extracted["name"], "arguments": extracted["arguments"]}]
    errors: list[str] = []
    if extracted["error"]:
        errors.append(extracted["error"])

    if not item.gold_calls:
        if calls:
            return ItemResult(
                item_id=item.item_id,
                category=item.category,
                model_used="",
                bucket="false_call",
                emitted=True,
                mode=extracted["mode"],
                n_predicted_calls=len(calls),
                n_gold_calls=0,
                ast_match=False,
                errors=(
                    f"called {sorted(str(c['name']) for c in calls)} where no call was warranted",
                ),
                predicted_calls=tuple(calls),
                gold_calls=item.gold_calls,
            )
        return ItemResult(
            item_id=item.item_id,
            category=item.category,
            model_used="",
            bucket="correct_abstain",
            emitted=False,
            mode=extracted["mode"],
            n_predicted_calls=0,
            n_gold_calls=0,
            ast_match=False,
            errors=(),
            gold_calls=item.gold_calls,
        )

    if not calls:
        return ItemResult(
            item_id=item.item_id,
            category=item.category,
            model_used="",
            bucket="no_call",
            emitted=False,
            mode=extracted["mode"],
            n_predicted_calls=0,
            n_gold_calls=len(item.gold_calls),
            ast_match=False,
            errors=tuple(errors) or ("no call emitted",),
            gold_calls=item.gold_calls,
        )

    gold_names = {str(call["name"]) for call in item.gold_calls}
    pred_names = {str(call.get("name")) for call in calls}
    if not pred_names <= gold_names:
        return ItemResult(
            item_id=item.item_id,
            category=item.category,
            model_used="",
            bucket="wrong_function",
            emitted=True,
            mode=extracted["mode"],
            n_predicted_calls=len(calls),
            n_gold_calls=len(item.gold_calls),
            ast_match=False,
            errors=(f"called {sorted(pred_names)}; gold is {sorted(gold_names)}",),
            predicted_calls=tuple(calls),
            gold_calls=item.gold_calls,
        )

    schema_errors: list[str] = []
    for call in calls:
        schema_errors.extend(
            f"{call.get('name')}: {message_}"
            for message_ in validate_arguments(specs[str(call["name"])], call.get("arguments"))
        )
    if schema_errors:
        return ItemResult(
            item_id=item.item_id,
            category=item.category,
            model_used="",
            bucket="invalid_arguments",
            emitted=True,
            mode=extracted["mode"],
            n_predicted_calls=len(calls),
            n_gold_calls=len(item.gold_calls),
            ast_match=False,
            errors=tuple(schema_errors),
            predicted_calls=tuple(calls),
            gold_calls=item.gold_calls,
        )

    matched = ast_match(calls, item.gold_calls)
    return ItemResult(
        item_id=item.item_id,
        category=item.category,
        model_used="",
        bucket="ast_match" if matched else "ast_mismatch",
        emitted=True,
        mode=extracted["mode"],
        n_predicted_calls=len(calls),
        n_gold_calls=len(item.gold_calls),
        ast_match=matched,
        errors=() if matched else tuple(ast_diff(item.gold_calls, calls)),
        predicted_calls=tuple(calls),
        gold_calls=item.gold_calls,
    )


def run_item(
    item: BatteryItem,
    *,
    specs: dict[str, Any],
    functions: list[dict[str, Any]],
    free_model: str = FREE_MODEL,
    paid_model: str = PAID_MODEL,
    api_key: str | None = None,
    timeout: float = 90.0,
    allow_paid_fallback: bool = True,
) -> ItemResult:
    """Call gemma for one item and score it. A transport failure is a bucket."""
    key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is unset; the battery cannot call gemma")
    tool_names = set(specs)
    openai_tools = [{"type": "function", "function": function} for function in functions]
    fallback_reason = ""
    last_error = ""
    for attempt_model in (free_model, paid_model) if allow_paid_fallback else (free_model,):
        try:
            message, prompt_tokens, completion_tokens, elapsed = chat_once(
                item.user, openai_tools, model=attempt_model, api_key=key, timeout=timeout
            )
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            last_error = f"HTTP {exc.code}"
            try:
                last_error += " " + exc.read().decode()[:200]
            except Exception:
                pass
            if _looks_exhausted(last_error) and attempt_model == free_model:
                fallback_reason = f"free pool exhausted: {last_error[:120]}"
                continue
            break
        except Exception as exc:  # pragma: no cover - network path
            last_error = f"{type(exc).__name__}: {exc}"
            if _looks_exhausted(last_error) and attempt_model == free_model:
                fallback_reason = f"free pool exhausted: {last_error[:120]}"
                continue
            break
        result = classify(item, message, tool_names, specs)
        result.model_used = attempt_model
        result.prompt_tokens = prompt_tokens
        result.completion_tokens = completion_tokens
        result.cost_usd = price_usd(attempt_model, prompt_tokens, completion_tokens)
        result.latency_ms = elapsed
        result.fallback_reason = fallback_reason
        return result
    failed = ItemResult(
        item_id=item.item_id,
        category=item.category,
        model_used=paid_model if fallback_reason else free_model,
        bucket="transport_error",
        emitted=False,
        mode="transport_error",
        n_predicted_calls=0,
        n_gold_calls=len(item.gold_calls),
        ast_match=False,
        errors=(last_error or "transport error",),
        gold_calls=item.gold_calls,
        fallback_reason=fallback_reason,
    )
    return failed


# --------------------------------------------------------------------------- #
# Reporting — per-item rows, buckets, the 007 table shaper
# --------------------------------------------------------------------------- #


def render_rows(results: Sequence[ItemResult]) -> str:
    """The per-item table: one row per item, never a dropped row."""
    header = (
        f"{'item':<28} {'category':<12} {'calls':<7} {'mode':<10} {'bucket':<18} "
        f"{'ast':<6} {'result':<7} detail"
    )
    lines = [header, "-" * len(header)]
    for result in results:
        calls = f"{result.n_predicted_calls}/{result.n_gold_calls}"
        verdict = "PASS" if result.scored_correct else "FAIL"
        detail = "; ".join(result.errors)[:110] or "-"
        lines.append(
            f"{result.item_id:<28} {result.category:<12} {calls:<7} {result.mode:<10} "
            f"{result.bucket:<18} {result.ast_match!s:<6} {verdict:<7} {detail}"
        )
    return "\n".join(lines)


def bucket_counts(results: Sequence[ItemResult]) -> dict[str, int]:
    counts = {bucket: 0 for bucket in BUCKETS}
    for result in results:
        counts[result.bucket] += 1
    return counts


def summarize(results: Sequence[ItemResult], *, model_label: str = "gemma-4-26b-a4b-it") -> dict[str, Any]:
    """Exact counts, the closed-bucket invariant, and the metered spend."""
    call_results = [r for r in results if r.is_call_item]
    irrelevance = [r for r in results if not r.is_call_item]
    scored = bfcl_ast_accuracy(
        [
            AstComparison(pred_calls=tuple(r.predicted_calls), gold_calls=r.gold_calls)
            for r in call_results
        ]
    )
    counts = bucket_counts(results)
    return {
        "label": OURS_PENDING_OFFICIAL,
        "model": model_label,
        "n_items": len(results),
        "n_call_items": len(call_results),
        "n_irrelevance_items": len(irrelevance),
        # The contract-shaped headline. Never labelled official.
        "ours_ast_accuracy": scored,
        "n_correct": sum(1 for r in results if r.scored_correct),
        "buckets": counts,
        # Metric: unparseable-as-non-call. The closed bucket set plus this sum
        # means every output is scored exactly once — nothing falls between
        # buckets and nothing is dropped.
        "unparseable_as_non_call": len(results) - sum(counts.values()),
        "by_category": {
            category: {
                "n": sum(1 for r in results if r.category == category),
                "n_correct": sum(1 for r in results if r.category == category and r.scored_correct),
            }
            for category in ("simple", "multiple", "parallel", "irrelevance")
        },
        "models_used": sorted({r.model_used for r in results if r.model_used}),
        "fallbacks": [
            {"item_id": r.item_id, "reason": r.fallback_reason}
            for r in results
            if r.fallback_reason
        ],
        "spend": {
            "prompt_tokens": sum(r.prompt_tokens for r in results),
            "completion_tokens": sum(r.completion_tokens for r in results),
            "usd": round(sum(r.cost_usd for r in results), 6),
            "paid_items": sum(1 for r in results if r.model_used and not r.model_used.endswith(":free")),
        },
    }


def leaderboard_rows(summary: dict[str, Any]) -> list[list[str]]:
    """The 007 table shaper: official column ``pending``, ours labelled.

    The official ``bfcl_ast_accuracy`` cell stays ``pending`` because the 007
    placeholder is still standing — proof that no number of ours is wearing an
    official label.
    """
    row = lb.LeaderboardRow(dataset="bfcl-v3-specs", model=summary["model"])
    row.official["bfcl_ast_accuracy"] = lb.run_official("bfcl_ast_accuracy")
    accuracy = summary["ours_ast_accuracy"]["ast_accuracy"]
    row.ours[f"ast_accuracy_{OURS_PENDING_OFFICIAL}"] = lb.Cell(
        value=round(100.0 * accuracy, 1), kind=lb.OURS_ONLY
    )
    # A category with no items has no rate, so it gets no column rather than a
    # fabricated 0.0.
    scored_categories = [
        category
        for category in ("simple", "multiple", "parallel", "irrelevance")
        if summary["by_category"][category]["n"] > 0
    ]
    for category in scored_categories:
        stats = summary["by_category"][category]
        row.ours[f"{category}_{OURS_PENDING_OFFICIAL}"] = lb.Cell(
            value=round(100.0 * (stats["n_correct"] / stats["n"]), 1), kind=lb.OURS_ONLY
        )
    lb.assert_columns_separate(row)
    return lb.shape_table(
        [row],
        ["bfcl_ast_accuracy"],
        [f"ast_accuracy_{OURS_PENDING_OFFICIAL}"]
        + [f"{c}_{OURS_PENDING_OFFICIAL}" for c in scored_categories],
    )


def run_battery(
    items: Sequence[BatteryItem] = BATTERY,
    *,
    timeout: float = 90.0,
    allow_paid_fallback: bool = True,
) -> tuple[list[ItemResult], dict[str, Any]]:
    """Run every item (all of them, always) and summarize."""
    specs = real_specs()
    functions = bfcl_functions()
    results = [
        run_item(
            item,
            specs=specs,
            functions=functions,
            timeout=timeout,
            allow_paid_fallback=allow_paid_fallback,
        )
        for item in items
    ]
    return results, summarize(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T-EVAL-006 BFCL-style battery on gemma")
    parser.add_argument("--out", default=str(pathlib.Path(__file__).with_suffix(".results.json")))
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--no-paid-fallback", action="store_true")
    args = parser.parse_args(argv)

    results, summary = run_battery(
        timeout=args.timeout, allow_paid_fallback=not args.no_paid_fallback
    )
    print(f"label: {OURS_PENDING_OFFICIAL} (official BFCL script NOT vendored)\n")
    print(render_rows(results))
    print("\nbuckets:", json.dumps(summary["buckets"]))
    print(
        "\nleaderboard (007 shaper; official column is `pending` until vendored):"
    )
    for line in leaderboard_rows(summary):
        print(" ", line)
    print("\n" + json.dumps(summary, indent=2))
    report = {
        "summary": summary,
        "results": [dataclasses.asdict(result) for result in results],
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
