"""T-EVAL-009 — gemma function-call probe (the gate before T-EVAL-006).

Question this answers: **can gemma CALL at all?** T-EVAL-006 would otherwise
spend a whole battery on a model that answers "I will search for that" in
prose and emits nothing dispatchable. This probe runs 10 prompts over the REAL
V3 tool schemas and asserts three things per prompt — nothing about quality:

  (a) a call is EMITTED (a dispatchable call, not prose about calling),
  (b) it PARSES as JSON and carries the right function name,
  (c) its arguments validate against the real ``ToolSpec.input_schema``.

**007 MAPPING CHECKED FIRST.** The T-EVAL-007 harness
(``evals/leaderboard.py`` + ``evals/MAPPING.md``) classifies every eval metric
as ``official`` (vendored upstream code) or ``ours_only`` — and the
deterministic lexical groundedness proxy is OURS-ONLY, never blended into an
official column. None of those metrics apply here: there is no corpus, no
retrieval, no reference answer and no scoring. ``groundedness_lexical_proxy``
is deliberately NOT used: this is a well-formedness probe, and mixing a quality
proxy into it would be exactly the blend 007 forbids. So the probe asserts
*well-formedness only*, never quality, and says so in its output.

Pass bar: **80 % well-formed over the call-expected prompts**. Below that,
T-EVAL-006 must NOT run on gemma — the probe prints the failure table and exits
non-zero (BLOCKED), which is the "do not burn the battery" branch.

GEMMA-ONLY per the wave's user setup: ``google/gemma-4-26b-a4b-it:free``
first; if the free pool is exhausted the probe falls back to the paid
``google/gemma-4-26b-a4b-it`` automatically and **reports every dollar it
spends**. Nothing here is product code and nothing is ingested.

Run (from repo root):
    /root/ko-venv/bin/python3 evals/gemma_call_probe.py            # live probe
    /root/ko-venv/bin/python3 evals/gemma_call_probe.py --dry-run  # offline, no network
    /root/ko-venv/bin/pytest evals/test_gemma_call_probe.py -q
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, cast

REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (REPO / "packages", REPO / "apps" / "api", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.runtime import AgentRuntime  # noqa: E402
from agents.tools import (  # noqa: E402
    CalculatorTool,
    KnowledgeReadTool,
    KnowledgeSearchTool,
    Tool,
    ToolRegistry,
)

# --------------------------------------------------------------------------- #
# Models, meter, bar
# --------------------------------------------------------------------------- #

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

#: The free model first (per the wave's user setup).
FREE_MODEL = "google/gemma-4-26b-a4b-it:free"
#: Paid fallback, used ONLY when the free pool is exhausted. Price per token as
#: published by the OpenRouter models endpoint (checked 2026-10-14).
PAID_MODEL = "google/gemma-4-26b-a4b-it"
PAID_PRICE_PER_TOKEN = {"prompt": 0.00000009, "completion": 0.00000030}

#: The gate: fraction of call-expected prompts that must be well-formed.
WELL_FORMED_BAR = 0.80

#: Exhaustion signals that trigger the paid fallback (wave rule).
_EXHAUSTION_MARKERS = (
    "exhausted",
    "no endpoints",
    "no available endpoints",
    "provider returned error",
    "rate limit",
    "rate-limited",
    "429",
    "402",
    "insufficient",
)

_UUID_A = "11111111-1111-4111-8111-111111111111"
_UUID_B = "22222222-2222-4222-8222-222222222222"
_UUID_C = "33333333-3333-4333-8333-333333333333"


# --------------------------------------------------------------------------- #
# The REAL V3 tool specs (read from packages/agents/tools.py, not invented)
# --------------------------------------------------------------------------- #


def real_specs() -> dict[str, Any]:
    """The three real V3 ``ToolSpec``s, derived by the runtime's own code.

    ``AgentRuntime.spec_for`` does the derivation (JSON round-trip, object
    schemas pinned to ``additionalProperties: false``) and touches no instance
    state, so it is called unbound against tools the real ``ToolRegistry``
    holds. Nothing here invents a schema shape.
    """
    registry = ToolRegistry()
    tools: tuple[Tool, ...] = (CalculatorTool(), KnowledgeSearchTool(), KnowledgeReadTool())
    for tool in tools:
        registry.register(tool)
    derive = cast(Any, AgentRuntime.spec_for)
    return {name: derive(None, registry.get(name)) for name in (t.name for t in tools)}


def openai_tools(specs: dict[str, Any]) -> list[dict[str, Any]]:
    """The same specs in OpenAI/OpenRouter function-calling shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            },
        }
        for spec in specs.values()
    ]


# --------------------------------------------------------------------------- #
# Prompts (schemas + prompts only; no corpus, no gold)
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class PromptCase:
    """One probe prompt. ``expected_tool is None`` for a negative control."""

    prompt_id: str
    user: str
    expected_tool: str | None
    note: str = ""


PROBE_PROMPTS: tuple[PromptCase, ...] = (
    PromptCase(
        "calc-basic",
        "Use the calculator tool: what is 12 * 45 + 10?",
        "calculator",
        "one required string arg",
    ),
    PromptCase(
        "calc-precedence",
        "Use the calculator to work out (2 + 3) * 4 - 6 / 3.",
        "calculator",
        "parenthesised expression",
    ),
    PromptCase(
        "calc-powers",
        "Use the calculator: compute 2 ** 10 + 1.",
        "calculator",
        "exponent operator",
    ),
    PromptCase(
        "search-query-only",
        "Use the knowledge search tool to find the termination notice period in "
        "the supplier contract.",
        "knowledge_search",
        "single required string arg",
    ),
    PromptCase(
        "search-with-topk",
        "Use the knowledge search tool with top_k set to 3 to find every "
        "mention of the SLA penalty clause.",
        "knowledge_search",
        "optional integer arg — must be an int, not \"3\"",
    ),
    PromptCase(
        "search-unicode",
        "Use the knowledge search tool to look up the Größenordnung of the "
        "Ümsatzsteuer calculation.",
        "knowledge_search",
        "non-ascii argument text",
    ),
    PromptCase(
        "read-triple",
        "Use the knowledge read tool to read document "
        f"{_UUID_A} version {_UUID_B} chunk {_UUID_C}.",
        "knowledge_read",
        "three required UUID-pattern strings",
    ),
    PromptCase(
        "read-in-uk-prose",
        "Before answering, you must read chunk id "
        f"{_UUID_C} of version {_UUID_B} in document {_UUID_A} "
        "using the knowledge read tool.",
        "knowledge_read",
        "args given out of order in the prompt",
    ),
    # --- negative controls: a correct model emits NO call here ---------------
    PromptCase(
        "no-call-plain",
        "In one short sentence, what is the capital of France? Do not use any "
        "tools; answer from your own knowledge.",
        None,
        "negative control: no tool is warranted",
    ),
    PromptCase(
        "no-call-refusal",
        "Do not call any tool. Just reply with the single word: ready.",
        None,
        "negative control: explicit no-tools instruction",
    ),
)


# --------------------------------------------------------------------------- #
# Extraction + validation (the three assertions)
# --------------------------------------------------------------------------- #

#: Phrases that mark a response as prose ABOUT calling rather than a call.
_PROSE_MARKERS = (
    "i will call",
    "i'll call",
    "let me call",
    "i will use the",
    "i can use the",
    "i would use the",
    "calling the",
    "tool call:",
    "action:",
)


def _strip_fences(text: str) -> str:
    """Drop ``` fences and <tool_call> wrappers, keep the inner text."""
    cleaned = text.strip()
    for tag in ("```json", "```JSON", "```"):
        cleaned = cleaned.replace(tag, " ")
    cleaned = re.sub(r"</?tool_call>", " ", cleaned)
    return cleaned.strip()


def _json_objects(text: str) -> list[Any]:
    """Every top-level JSON object in a text blob (brace-balanced scan)."""
    found: list[Any] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        found.append(json.loads(text[start : index + 1]))
                    except json.JSONDecodeError:
                        found.append(None)
                    start = -1
    return found


def _call_from_payload(payload: Any, tool_names: set[str]) -> tuple[str | None, Any, bool]:
    """``(function_name, arguments, recognised)`` from one parsed payload.

    Accepts the two shapes models actually emit — the OpenAI wrapper
    (``{"name": ..., "arguments": "{json string}"}``) and a flat call object —
    plus a ``{"function": {...}}`` envelope. Anything else is unrecognised.
    """
    if not isinstance(payload, dict):
        return None, None, False
    node: Any = payload
    if "function" in node and isinstance(node["function"], dict):
        node = node["function"]
    name = node.get("name") or node.get("tool") or node.get("function_name")
    if not isinstance(name, str):
        return None, None, False
    arguments = node.get("arguments", node.get("parameters", node.get("args")))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return name, None, True
    if name not in tool_names:
        return name, arguments, True
    return name, arguments, True


def extract_call(message: dict[str, Any], tool_names: set[str]) -> dict[str, Any]:
    """Assertion (a)+(b): what did the model actually emit?

    Returns ``{"emitted", "mode", "name", "arguments", "error"}``. A prose
    answer, a refusal, or a JSON blob that is not a call all come back with
    ``emitted=False`` and a recorded ``error`` — never as a silent pass and
    never dropped.
    """
    native = message.get("tool_calls") or []
    if native:
        first = native[0]
        function = first.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return {
                    "emitted": False,
                    "mode": "native_unparseable_arguments",
                    "name": function.get("name"),
                    "arguments": None,
                    "error": "native tool_calls arguments are not JSON",
                }
        return {
            "emitted": True,
            "mode": "native",
            "name": function.get("name"),
            "arguments": arguments,
            "error": "",
        }
    text = (message.get("content") or "").strip()
    if not text:
        return {
            "emitted": False,
            "mode": "empty",
            "name": None,
            "arguments": None,
            "error": "empty response",
        }
    blob = _strip_fences(text)
    for payload in _json_objects(blob):
        name, arguments, recognised = _call_from_payload(payload, tool_names)
        if recognised and isinstance(name, str):
            if name not in tool_names:
                return {
                    "emitted": False,
                    "mode": "text_unknown_tool",
                    "name": name,
                    "arguments": arguments,
                    "error": f"called unknown tool {name!r}",
                }
            return {
                "emitted": True,
                "mode": "text_json",
                "name": name,
                "arguments": arguments,
                "error": "",
            }
    lowered = blob.lower()
    if any(marker in lowered for marker in _PROSE_MARKERS):
        return {
            "emitted": False,
            "mode": "prose_about_calling",
            "name": None,
            "arguments": None,
            "error": "prose about calling, no dispatchable call",
        }
    if _json_objects(blob):
        return {
            "emitted": False,
            "mode": "text_json_not_a_call",
            "name": None,
            "arguments": None,
            "error": "JSON emitted but it is not a function call",
        }
    return {
        "emitted": False,
        "mode": "prose_only",
        "name": None,
        "arguments": None,
        "error": "no call and no JSON in the response",
    }


def validate_arguments(spec: Any, arguments: Any) -> list[str]:
    """Assertion (c): arguments against the REAL ``ToolSpec.input_schema``."""
    if not isinstance(arguments, dict):
        return [f"arguments are {type(arguments).__name__}, not an object"]
    import jsonschema

    validator = jsonschema.Draft202012Validator(spec.input_schema)
    return [error.message for error in sorted(validator.iter_errors(arguments), key=str)]


# --------------------------------------------------------------------------- #
# Live probe
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class ProbeResult:
    """One prompt's outcome. Every prompt produces exactly one of these."""

    prompt_id: str
    kind: str  # "call" | "no_call"
    expected_tool: str | None
    model_used: str
    mode: str
    emitted: bool
    name_ok: bool
    args_ok: bool
    well_formed: bool
    arguments: Any = None
    errors: tuple[str, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    fallback_reason: str = ""

    @property
    def correct(self) -> bool:
        """A negative control is correct when it emits nothing."""
        return self.well_formed if self.kind == "call" else not self.emitted


def _price_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    if model.endswith(":free"):
        return 0.0
    return round(
        prompt_tokens * PAID_PRICE_PER_TOKEN["prompt"]
        + completion_tokens * PAID_PRICE_PER_TOKEN["completion"],
        6,
    )


def _post(
    payload: dict[str, Any],
    *,
    api_key: str,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/knowledge-os",
            "X-Title": "knowledge-os T-EVAL-009 gemma call probe",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def chat_once(
    user_prompt: str,
    tools: list[dict[str, Any]],
    *,
    model: str,
    api_key: str,
    timeout: float = 90.0,
) -> tuple[dict[str, Any], int, int, int]:
    """One chat completion. Returns ``(message, prompt_tokens, completion, ms)``."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": user_prompt}],
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": 400,
    }
    started = time.monotonic()
    body = _post(payload, api_key=api_key, timeout=timeout)
    elapsed = int((time.monotonic() - started) * 1000)
    usage = body.get("usage") or {}
    message = (body.get("choices") or [{}])[0].get("message") or {}
    return (
        message,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        elapsed,
    )


def _is_exhausted(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _EXHAUSTION_MARKERS)


def probe_case(
    case: PromptCase,
    specs: dict[str, Any],
    *,
    free_model: str = FREE_MODEL,
    paid_model: str = PAID_MODEL,
    api_key: str | None = None,
    timeout: float = 90.0,
    allow_paid_fallback: bool = True,
) -> ProbeResult:
    """Run one prompt against gemma and record a FAIL for anything unusable."""
    key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is unset; the probe cannot call gemma")
    tools = openai_tools(specs)
    tool_names = set(specs)
    model = free_model
    fallback_reason = ""
    last_error = ""
    for attempt_model in (free_model, paid_model) if allow_paid_fallback else (free_model,):
        try:
            message, prompt_tokens, completion_tokens, elapsed = chat_once(
                case.user, tools, model=attempt_model, api_key=key, timeout=timeout
            )
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            last_error = f"HTTP {exc.code}"
            try:
                last_error += " " + exc.read().decode()[:200]
            except Exception:
                pass
            if _is_exhausted(last_error) and attempt_model == free_model:
                model = paid_model
                fallback_reason = f"free pool exhausted: {last_error[:120]}"
                continue
            break
        except Exception as exc:  # pragma: no cover - network path
            last_error = f"{type(exc).__name__}: {exc}"
            if _is_exhausted(last_error) and attempt_model == free_model:
                model = paid_model
                fallback_reason = f"free pool exhausted: {last_error[:120]}"
                continue
            break
        extracted = extract_call(message, tool_names)
        errors: list[str] = []
        if extracted["error"]:
            errors.append(extracted["error"])
        expected = case.expected_tool
        name_ok = extracted["emitted"] and extracted["name"] == expected
        if expected is not None:
            if not extracted["emitted"]:
                pass  # already recorded above
            elif extracted["name"] != expected:
                errors.append(f"called {extracted['name']!r}, expected {expected!r}")
        schema_errors = (
            validate_arguments(specs[expected], extracted["arguments"])
            if name_ok and expected is not None
            else []
        )
        errors.extend(schema_errors)
        return ProbeResult(
            prompt_id=case.prompt_id,
            kind="call" if expected else "no_call",
            expected_tool=expected,
            model_used=attempt_model,
            mode=extracted["mode"],
            emitted=bool(extracted["emitted"]),
            name_ok=bool(name_ok),
            args_ok=not schema_errors,
            well_formed=bool(extracted["emitted"]) and bool(name_ok) and not schema_errors,
            arguments=extracted["arguments"],
            errors=tuple(errors),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=_price_usd(attempt_model, prompt_tokens, completion_tokens),
            latency_ms=elapsed,
            fallback_reason=fallback_reason,
        )
    return ProbeResult(
        prompt_id=case.prompt_id,
        kind="call" if case.expected_tool else "no_call",
        expected_tool=case.expected_tool,
        model_used=model,
        mode="transport_error",
        emitted=False,
        name_ok=False,
        args_ok=False,
        well_formed=False,
        errors=(last_error or "transport error",),
        fallback_reason=fallback_reason,
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_table(results: list[ProbeResult]) -> str:
    """The per-prompt table. One row per prompt — never a dropped non-call."""
    header = (
        f"{'prompt':<20} {'kind':<8} {'expected':<17} {'mode':<22} "
        f"{'emitted':<8} {'name_ok':<8} {'args_ok':<8} {'result':<8} detail"
    )
    lines = [header, "-" * len(header)]
    for result in results:
        verdict = "PASS" if result.correct else "FAIL"
        detail = "; ".join(result.errors) or "-"
        tool = result.expected_tool or "-"
        lines.append(
            f"{result.prompt_id:<20} {result.kind:<8} {tool:<17} "
            f"{result.mode:<22} {result.emitted!s:<8} {result.name_ok!s:<8} "
            f"{result.args_ok!s:<8} {verdict:<8} {detail}"
        )
    return "\n".join(lines)


def summarize(results: list[ProbeResult]) -> dict[str, Any]:
    """Exact counts for the two ticket metrics + the meter."""
    call_results = [r for r in results if r.kind == "call"]
    negative = [r for r in results if r.kind == "no_call"]
    well_formed = [r for r in call_results if r.well_formed]
    rate = (len(well_formed) / len(call_results)) if call_results else 0.0
    return {
        "n_prompts": len(results),
        "n_call_expected": len(call_results),
        "n_well_formed": len(well_formed),
        "well_formed_rate": round(rate, 4),
        "bar": WELL_FORMED_BAR,
        "verdict": "PASS" if rate >= WELL_FORMED_BAR else "BLOCKED",
        # Metric: silent non-calls. Every non-well-formed prompt is a recorded
        # FAIL row with at least one error string; none are dropped.
        "n_silent_non_calls": 0
        if all(r.well_formed or r.errors for r in call_results)
        else 1,
        "n_failures": len(call_results) - len(well_formed),
        "n_negative_controls": len(negative),
        "n_negative_controls_correct": sum(1 for r in negative if r.correct),
        "non_call_modes": sorted({r.mode for r in call_results if not r.well_formed}),
        "models_used": sorted({r.model_used for r in results}),
        "fallbacks": [
            {"prompt_id": r.prompt_id, "reason": r.fallback_reason}
            for r in results
            if r.fallback_reason
        ],
        "spend": {
            "prompt_tokens": sum(r.prompt_tokens for r in results),
            "completion_tokens": sum(r.completion_tokens for r in results),
            "usd": round(sum(r.cost_usd for r in results), 6),
            "paid_prompts": sum(1 for r in results if not r.model_used.endswith(":free")),
        },
    }


def run_probe(
    prompts: tuple[PromptCase, ...] = PROBE_PROMPTS,
    *,
    allow_paid_fallback: bool = True,
    timeout: float = 90.0,
) -> tuple[list[ProbeResult], dict[str, Any]]:
    """Run every prompt (all of them, always) and summarize."""
    specs = real_specs()
    results = [
        probe_case(
            case,
            specs,
            timeout=timeout,
            allow_paid_fallback=allow_paid_fallback,
        )
        for case in prompts
    ]
    return results, summarize(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T-EVAL-009 gemma function-call probe")
    parser.add_argument("--out", default=str(pathlib.Path(__file__).with_suffix(".results.json")))
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--no-paid-fallback", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the specs + prompts and exit; no network call",
    )
    args = parser.parse_args(argv)

    specs = real_specs()
    if args.dry_run:
        print("real V3 specs (from packages/agents/tools.py):")
        for spec in specs.values():
            print(f"  {spec.name}: {json.dumps(spec.input_schema)[:200]}")
        print(f"\nprompts: {len(PROBE_PROMPTS)}")
        for case in PROBE_PROMPTS:
            print(f"  {case.prompt_id:<20} -> {case.expected_tool}")
        return 0

    results, summary = run_probe(
        timeout=args.timeout, allow_paid_fallback=not args.no_paid_fallback
    )
    print(render_table(results))
    print()
    print(json.dumps(summary, indent=2))
    report = {"summary": summary, "results": [dataclasses.asdict(r) for r in results]}
    pathlib.Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}")
    if summary["verdict"] == "BLOCKED":
        print(
            "BLOCKED: well-formed rate below the 80% bar — do NOT run T-EVAL-006 on gemma.\n"
            "Failing shapes: " + ", ".join(summary["non_call_modes"])
        )
        return 1
    print("PASS: gemma emits well-formed calls; T-EVAL-006 may run on gemma.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())