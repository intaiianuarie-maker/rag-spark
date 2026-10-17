"""BFCL-style AST exact-match — **OURS-PENDING-OFFICIAL** (T-EVAL-006).

WHAT THIS IS NOT
----------------
This is **not** the Berkeley Function-Calling Leaderboard's official scorer.
The official script is **not vendored**; the T-EVAL-007 harness still holds the
``bfcl_ast_accuracy`` placeholder (``evals/vendor/placeholders.py``,
registry entry ``bfcl_ast_metrics``), and every number this module produces is
labelled :data:`OURS_PENDING_OFFICIAL` so it can never be read as an official
BFCL number. The 007 mapping doc rule applies unchanged: official means
vendored upstream code, ours-only means ours, and nothing is blended.

WHAT IT IS
----------
An implementation of the AST-match comparison as specified in the BFCL paper /
leaderboard docs, shaped **exactly** like the placeholder contract so that
vendoring later is a module swap, never a redesign:

    bfcl_ast_accuracy(per_item: list[AstComparison]) -> {"total", "correct", "ast_accuracy"}

with ``AstComparison(pred_calls, gold_calls)`` where a call is
``{"name": str, "arguments": {...}}``, the call set compared order-insensitively
(one-to-one matching, so a duplicate prediction cannot satisfy two gold calls),
and recursive comparison of numbers/strings/bools/lists/dicts with the upstream's
documented type coercions.

THE SWAP SEAM (do this when the network is available)
----------------------------------------------------
1. vendor the official script verbatim to :data:`SWAP_TARGET`
   (``evals/vendor/bfcl_eval.py``) and register it in ``evals/vendor/__init__.py``;
2. delete the ``bfcl_ast_metrics`` placeholder entry;
3. point :data:`SCORER_MODULE`/``evals/bfcl_battery.py``'s scorer at the
   vendored ``bfcl_ast_accuracy`` and DELETE the OURS_PENDING_OFFICIAL label;
4. re-run ``evals/bfcl_battery.py``.

Nothing else moves: the battery, the buckets, the per-item rows and the table
shaper are all agnostic to which scorer ran. ``evals/test_bfcl_battery.py``
pins the contract keys so a swapped-in scorer that does not honour them fails
loudly instead of silently producing incomparable numbers.

Documented comparison rules (the leniencies the upstream applies)
----------------------------------------------------------------
1. identical JSON values match;
2. ``2`` matches ``2.0`` (int/float are one numeric kind);
3. a *string* that renders the same as the other side matches (models return
   numbers as strings constantly, and the upstream is lenient there), but only
   after the strict numeric comparison of rule 2 has failed;
4. booleans match booleans, and the strings ``"true"/"false"/"True"/"False"``
   match a boolean (case-insensitively) — ``True`` never matches ``1``;
5. ``None`` matches ``None`` and the strings ``"null"/"None"/""``;
6. lists match as **multisets** by default (the leaderboard's default AST mode
   is order-insensitive); ``strict_list=True`` switches to order-sensitive;
7. dicts match when their key sets are equal and every value matches recursively;
8. any key present in the prediction but not in the gold (or vice versa) is a
   mismatch — no extra arguments, no missing ones.
"""

from __future__ import annotations

import collections
from collections.abc import Mapping, Sequence
from typing import NamedTuple

#: The label every number from this module must carry until the official script
#: is vendored. Printed in the table header and asserted by the tests.
OURS_PENDING_OFFICIAL = "OURS-PENDING-OFFICIAL"

#: Where the official implementation must land, and which registry entry it
#: replaces. See the module docstring's swap steps.
SWAP_TARGET = "evals/vendor/bfcl_eval.py"
SWAP_REGISTRY_ENTRY = "bfcl_ast_metrics"
UPSTREAM = "gorilla/berkeley-function-call-leaderboard"

#: The contract this module implements (identical to the 007 placeholder's).
CONTRACT_KEYS = ("total", "correct", "ast_accuracy")

_TRUE_STRINGS = {"true", "True"}
_FALSE_STRINGS = {"false", "False"}
_NULL_STRINGS = {"null", "None", ""}


class AstComparison(NamedTuple):
    """One scored item: the predicted call set vs the gold call set."""

    pred_calls: tuple[dict[str, object], ...]
    gold_calls: tuple[dict[str, object], ...]


def _as_text(value: object) -> str | None:
    """The canonical text form used by the string/number leniency."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, str):
        return value
    return None


def _bool_from_text(value: str) -> bool | None:
    if value in _TRUE_STRINGS or value.lower() == "true":
        return True
    if value in _FALSE_STRINGS or value.lower() == "false":
        return False
    return None


def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def ast_equal(expected: object, actual: object, *, strict_list: bool = False) -> bool:
    """Recursive AST equality with the documented coercions."""
    # Rule 4 first: booleans never degrade into 0/1.
    if isinstance(expected, bool) or isinstance(actual, bool):
        if isinstance(expected, bool) and isinstance(actual, bool):
            return expected is actual
        other = actual if isinstance(expected, bool) else expected
        flag = expected if isinstance(expected, bool) else actual
        text = _as_text(other)
        if text is None or isinstance(other, bool):
            return False
        coerced = _bool_from_text(text)
        return coerced is not None and coerced is flag
    # Rule 5: null-ish strings.
    if expected is None or actual is None:
        other = actual if expected is None else expected
        if other is None:
            return True
        return isinstance(other, str) and other in _NULL_STRINGS
    # Rule 2: numbers of either width.
    if _is_number(expected) and _is_number(actual):
        return float(expected) == float(actual)
    # Rule 6: lists.
    if isinstance(expected, list) and isinstance(actual, list):
        if strict_list:
            return len(expected) == len(actual) and all(
                ast_equal(e, a, strict_list=strict_list) for e, a in zip(expected, actual, strict=True)
            )
        if len(expected) != len(actual):
            return False
        remaining = list(actual)
        for element in expected:
            for index, candidate in enumerate(remaining):
                if ast_equal(element, candidate, strict_list=strict_list):
                    remaining.pop(index)
                    break
            else:
                return False
        return True
    # Rule 7: dicts.
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        if set(expected) != set(actual):
            return False
        return all(ast_equal(expected[key], actual[key], strict_list=strict_list) for key in expected)
    # Rule 1 then rule 3.
    if isinstance(expected, str) and isinstance(actual, str):
        return expected == actual
    if type(expected) is type(actual):
        return expected == actual
    expected_text = _as_text(expected)
    actual_text = _as_text(actual)
    return expected_text is not None and expected_text == actual_text


def call_key(call: Mapping[str, object]) -> tuple[str, str]:
    """A hashable key for one call, used by the one-to-one set matching."""
    name = str(call.get("name", ""))
    arguments = call.get("arguments")
    if not isinstance(arguments, Mapping):
        arguments = {}
    return name, _stable(arguments)


def _stable(value: object) -> str:
    """A deterministic string form for nested argument values."""
    if isinstance(value, Mapping):
        return "{" + ",".join(f"{k}={_stable(value[k])}" for k in sorted(value)) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_stable(item) for item in value) + "]"
    return repr(value)


def ast_match(
    pred_calls: Sequence[Mapping[str, object]],
    gold_calls: Sequence[Mapping[str, object]],
    *,
    strict_list: bool = False,
) -> bool:
    """Every gold call is matched one-to-one by an AST-equal predicted call.

    Order-insensitive on the call set (the upstream default), and one-to-one so
    predicting the same call twice cannot satisfy two gold calls.
    """
    remaining = list(pred_calls)
    for gold in gold_calls:
        for index, candidate in enumerate(remaining):
            if str(candidate.get("name", "")) != str(gold.get("name", "")):
                continue
            gold_args = gold.get("arguments")
            pred_args = candidate.get("arguments")
            if not isinstance(gold_args, Mapping) or not isinstance(pred_args, Mapping):
                if gold_args == pred_args:
                    remaining.pop(index)
                    break
                continue
            if set(gold_args) != set(pred_args):
                continue
            if all(
                ast_equal(gold_args[key], pred_args[key], strict_list=strict_list) for key in gold_args
            ):
                remaining.pop(index)
                break
        else:
            return False
    return True


def _args_of(call: Mapping[str, object]) -> Mapping[str, object]:
    """The call's arguments as a mapping (``{}`` when absent or malformed)."""
    arguments = call.get("arguments")
    return arguments if isinstance(arguments, Mapping) else {}


def _mismatch_count(gold_args: Mapping[str, object], pred_args: Mapping[str, object]) -> int:
    """How many argument keys disagree — used to pick the closest prediction."""
    return sum(
        0 if ast_equal(gold_args.get(key), pred_args.get(key)) else 1
        for key in set(gold_args) | set(pred_args)
    )


def ast_diff(
    gold_calls: Sequence[Mapping[str, object]],
    pred_calls: Sequence[Mapping[str, object]],
) -> list[str]:
    """Human-readable reasons a call set did not match (for the report)."""
    reasons: list[str] = []
    gold_names = collections.Counter(str(call.get("name", "")) for call in gold_calls)
    pred_names = collections.Counter(str(call.get("name", "")) for call in pred_calls)
    for name in sorted(set(gold_names) | set(pred_names)):
        if gold_names[name] != pred_names[name]:
            reasons.append(f"{name}: {pred_names[name]} predicted vs {gold_names[name]} in gold")
    for gold in gold_calls:
        candidates = [
            call for call in pred_calls if str(call.get("name", "")) == str(gold.get("name", ""))
        ]
        if not candidates:
            continue
        gold_args: Mapping[str, object] = gold.get("arguments") or {}
        best = min(candidates, key=lambda call: _mismatch_count(gold_args, _args_of(call)))
        pred_args = _args_of(best)
        for key in sorted(set(gold_args) | set(pred_args)):
            if key not in gold_args:
                reasons.append(f"{gold.get('name')}: unexpected argument {key!r}")
            elif key not in pred_args:
                reasons.append(f"{gold.get('name')}: missing argument {key!r}")
            elif not ast_equal(gold_args[key], pred_args[key]):
                reasons.append(
                    f"{gold.get('name')}.{key}: predicted {pred_args[key]!r} "
                    f"!= gold {gold_args[key]!r}"
                )
    return reasons


def bfcl_ast_accuracy(
    per_item: Sequence[AstComparison],
    *,
    strict_list: bool = False,
) -> dict[str, float]:
    """**OURS-PENDING-OFFICIAL.** The 007 placeholder contract, honoured.

    Returns ``{"total": int, "correct": int, "ast_accuracy": float}`` where an
    item is correct when every gold call is matched one-to-one by an AST-equal
    predicted call, and the accuracy is ``correct / total`` (0.0 when there are
    no items). Swapping in the vendored official scorer means replacing this one
    function — the battery does not change.
    """
    total = len(per_item)
    correct = sum(
        1 for item in per_item if ast_match(item.pred_calls, item.gold_calls, strict_list=strict_list)
    )
    return {"total": total, "correct": correct, "ast_accuracy": (correct / total) if total else 0.0}


__all__ = [
    "CONTRACT_KEYS",
    "OURS_PENDING_OFFICIAL",
    "SWAP_REGISTRY_ENTRY",
    "SWAP_TARGET",
    "UPSTREAM",
    "AstComparison",
    "ast_diff",
    "ast_equal",
    "ast_match",
    "bfcl_ast_accuracy",
    "call_key",
]
