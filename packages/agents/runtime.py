"""Agent Runtime execution engine (Section 25) + V3 enforced dispatch (A1).

Executes the ReAct loop: Goal -> Plan -> Tool Call -> Observation -> Final Answer.
Maintains a durable step trace on the AgentRun object for replay and debugging.

V3 enforcement (rew1 B2/B3/B4): the legacy prototype dispatched any tool the
model named; policy was only reflected in the prompt. :meth:`run_enforced` is
the production path — it consults the server-side :class:`ToolPolicy` before
dispatch (default-deny), validates model arguments against the tool's strict
schema, rejects reserved-context override attempts, bounds every tool call by
timeout and output size, and reserves/enforces the run budget across steps.

`run` is kept as the prototype entrypoint for backwards compatibility. It builds
an *allow-all* context from :class:`AgentConfig`; production callers must use
`run_enforced` with a server-created :class:`RunContext`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import UTC, datetime

from models.interfaces import ChatMessage, GenerationRequest, LLMProvider, MessageRole

from .interfaces import (
    AgentConfig,
    AgentErrorCode,
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepType,
    ContextOverrideError,
    RunContext,
    RunState,
    RunStatus,
    StepStatus,
    ToolClass,
    ToolPolicy,
    ToolSpec,
    validate_tool_arguments,
)
from .tools import Tool, ToolRegistry


class ToolSchemaError(ValueError):
    """Raised when model-supplied tool arguments violate the tool's schema."""


def _tool_class_for(name: str) -> ToolClass:
    """Read tools by default; named write tools opt into the write class."""
    write_tools = {"knowledge_write", "web_write", "publish", "ingest", "delete"}
    return ToolClass.WRITE if name in write_tools else ToolClass.READ


def _legacy_status(status: RunStatus) -> AgentRunStatus:
    """Map the V3 durable status onto the legacy AgentRun status enum."""
    mapping = {
        RunStatus.QUEUED: AgentRunStatus.PENDING,
        RunStatus.RUNNING: AgentRunStatus.RUNNING,
        RunStatus.WAITING_INPUT: AgentRunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL: AgentRunStatus.RUNNING,
        RunStatus.COMPLETED: AgentRunStatus.COMPLETED,
        RunStatus.PARTIAL: AgentRunStatus.COMPLETED,
        RunStatus.FAILED: AgentRunStatus.FAILED,
        RunStatus.CANCELLED: AgentRunStatus.FAILED,
        RunStatus.LIMIT_EXCEEDED: AgentRunStatus.MAX_STEPS_EXCEEDED,
    }
    return mapping.get(status, AgentRunStatus.FAILED)


class AgentRuntime:
    """Executes multi-step agent workflows with tools and audit tracing."""

    def __init__(self, llm_provider: LLMProvider, tool_registry: ToolRegistry) -> None:
        self.llm = llm_provider
        self.registry = tool_registry

    # -- V3 enforcement (rew1 B2/B3/B4) ------------

    def spec_for(self, tool: Tool) -> ToolSpec:
        """Derive a strict :class:`ToolSpec` for a registered tool.

        The tool's declared ``input_schema`` becomes the contract; object
        schemas are pinned to ``additionalProperties: false`` so a model cannot
        smuggle extra arguments past dispatch.
        """
        schema = json.loads(json.dumps(tool.input_schema or {}))
        if schema.get("type") == "object" and "additionalProperties" not in schema:
            schema["additionalProperties"] = False
        return ToolSpec(
            name=tool.name,
            version="1",
            description=tool.description,
            input_schema=schema,
            tool_class=_tool_class_for(tool.name),
        )

    def _validate_arguments_against_schema(self, tool_input: dict, spec: ToolSpec) -> None:
        """Strict-schema check: required fields present, no undeclared properties."""
        schema = spec.input_schema
        if schema.get("additionalProperties") is False:
            allowed = set(schema.get("properties", {}))
            undeclared = set(tool_input) - allowed
            if undeclared:
                raise ToolSchemaError(
                    f"{spec.name}: undeclared properties rejected: {sorted(undeclared)}"
                )
        for required in schema.get("required", []):
            if required not in tool_input:
                raise ToolSchemaError(f"{spec.name}: missing required field {required!r}")

    async def _dispatch(
        self,
        *,
        context: RunContext,
        tool_name: str,
        raw_tool_input: str,
        policy: ToolPolicy,
        state: RunState,
    ) -> tuple[dict, StepStatus, AgentErrorCode | None]:
        """Enforced tool dispatch. Returns ``(output, status, error_code)``.

        Order matters: policy -> reserved context -> schema -> timeout -> output
        bound. A refusal is a BLOCKED step, never a silent success.
        """
        # B2: policy is consulted here, not only in the prompt.
        if not policy.allows(tool_name):
            return (
                {"error": f"Tool '{tool_name}' is not allowed by the run policy."},
                StepStatus.BLOCKED,
                AgentErrorCode.TOOL_NOT_ALLOWED,
            )

        tool = self.registry.get(tool_name)
        if tool is None:
            return (
                {"error": f"Tool '{tool_name}' not found."},
                StepStatus.BLOCKED,
                AgentErrorCode.TOOL_NOT_FOUND,
            )

        try:
            tool_input = json.loads(raw_tool_input)
            if not isinstance(tool_input, dict):
                raise ValueError("tool input must be a JSON object")
        except Exception:
            return (
                {"error": "Tool input was not a valid JSON object."},
                StepStatus.BLOCKED,
                AgentErrorCode.SCHEMA_INVALID,
            )

        # B3: reserved-context override guard.
        try:
            validate_tool_arguments(tool_input)
        except ContextOverrideError as exc:
            return (
                {"error": str(exc)},
                StepStatus.BLOCKED,
                AgentErrorCode.TOOL_NOT_ALLOWED,
            )

        spec = self.spec_for(tool)

        # B3: strict schema validation.
        try:
            self._validate_arguments_against_schema(tool_input, spec)
        except ToolSchemaError as exc:
            return {"error": str(exc)}, StepStatus.BLOCKED, AgentErrorCode.TOOL_SCHEMA_INVALID

        # B3/B4: wall-clock timeout and bounded output.
        try:
            result = await asyncio.wait_for(
                tool.execute(context.workspace_id, **tool_input),
                timeout=spec.timeout_s,
            )
        except TimeoutError:
            return (
                {"error": f"Tool '{tool_name}' exceeded {spec.timeout_s}s timeout."},
                StepStatus.FAILED,
                AgentErrorCode.TOOL_TIMEOUT,
            )
        except Exception as exc:
            return (
                {"error": f"Tool execution failed: {exc}"},
                StepStatus.FAILED,
                AgentErrorCode.PROVIDER_ERROR,
            )

        serialized = json.dumps(result, default=str)
        if len(serialized.encode()) > spec.max_output_bytes:
            return (
                {
                    "error": (
                        f"Tool output exceeded {spec.max_output_bytes} bytes; truncated."
                    )
                },
                StepStatus.FAILED,
                AgentErrorCode.TOOL_OUTPUT_TOO_LARGE,
            )
        if not isinstance(result, dict):
            result = {"result": result}
        return result, StepStatus.SUCCEEDED, None

    def _check_step_budget(
        self, *, state: RunState, context: RunContext, tool_calls_used: int
    ) -> bool:
        """Return True when the run may take another step, else set limit state."""
        if datetime.now(UTC) >= context.deadline:
            state.transition_to(
                RunStatus.LIMIT_EXCEEDED,
                error_code=AgentErrorCode.DEADLINE_EXCEEDED,
                error_detail="run deadline reached before step budget",
            )
            return False
        if tool_calls_used >= context.budget.max_tool_calls:
            state.transition_to(
                RunStatus.LIMIT_EXCEEDED,
                error_code=AgentErrorCode.BUDGET_EXCEEDED,
                error_detail=f"tool-call budget exceeded ({context.budget.max_tool_calls})",
            )
            return False
        return True

    async def run_enforced(
        self,
        *,
        context: RunContext,
        goal: str,
        instructions: str = (
            "You are a helpful knowledge agent. Solve user tasks step-by-step using tools."
        ),
    ) -> AgentRun:
        """Production entrypoint: every contract in :class:`RunContext` is enforced."""
        state = RunState(status=RunStatus.RUNNING)
        policy = ToolPolicy(allowed_tools=list(context.allowed_tools))
        model = context.model_policy.default_model or "gpt-4o-mini"

        agent_run = AgentRun(
            id=context.run_id,
            workspace_id=context.workspace_id,
            goal=goal,
            model=model,
            status=AgentRunStatus.RUNNING,
            created_at=datetime.now(UTC).isoformat(),
        )

        system_prompt = self._build_system_prompt_from(
            instructions, [t.name for t in self.registry.list_tools() if policy.allows(t.name)]
        )
        history: list[ChatMessage] = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=f"Goal: {goal}"),
        ]

        total_tokens = 0
        tool_calls = 0

        for _step_idx in range(1, context.budget.max_steps + 1):
            # B4: budget/deadline checked BEFORE any spend.
            if not self._check_step_budget(
                state=state, context=context, tool_calls_used=tool_calls
            ):
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail
                break

            req = GenerationRequest(
                model=model,
                messages=history,
                temperature=context.model_policy.temperature,
                max_tokens=context.model_policy.max_output_tokens,
            )
            try:
                gen_res = await self.llm.generate(req)
            except Exception as exc:
                state.mark_unknown_required_failure(f"provider failure: {exc}")
                state.transition_to(
                    RunStatus.FAILED,
                    error_code=AgentErrorCode.PROVIDER_ERROR,
                    error_detail=f"provider failure: {exc}",
                )
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail
                break

            step_tokens = (gen_res.prompt_tokens or 0) + (gen_res.completion_tokens or 0)
            total_tokens += step_tokens
            agent_run.total_tokens = total_tokens
            if total_tokens > context.budget.max_tokens:
                state.transition_to(
                    RunStatus.LIMIT_EXCEEDED,
                    error_code=AgentErrorCode.BUDGET_EXCEEDED,
                    error_detail=f"token budget exceeded ({total_tokens} > "
                    f"{context.budget.max_tokens})",
                )
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail
                break

            text = gen_res.text.strip()
            history.append(ChatMessage(role=MessageRole.ASSISTANT, content=text))

            action = re.search(r"Action:\s*([a-zA-Z0-9_\-]+)", text)
            action_input = re.search(r"Action Input:\s*(\{.*?\}|\[.*?\]|[^\n]+)", text, re.DOTALL)
            final_answer = re.search(r"Final Answer:\s*(.+)", text, re.DOTALL)

            if final_answer and not action:
                agent_run.final_output = final_answer.group(1).strip()
                state.transition_to(RunStatus.COMPLETED)
                agent_run.status = _legacy_status(state.status)
                break

            if action:
                tool_name = action.group(1).strip()
                raw_input = action_input.group(1).strip() if action_input else "{}"
                tool_calls += 1

                output, status, error_code = await self._dispatch(
                    context=context,
                    tool_name=tool_name,
                    raw_tool_input=raw_input,
                    policy=policy,
                    state=state,
                )

                agent_run.steps.append(
                    AgentStep(
                        step_number=len(agent_run.steps) + 1,
                        type=(
                            AgentStepType.TOOL_OUTPUT
                            if status is StepStatus.SUCCEEDED
                            else AgentStepType.ERROR
                        ),
                        content="",
                        tool_name=tool_name,
                        tool_input=(
                            {"raw": raw_input} if status is not StepStatus.SUCCEEDED else None
                        ),
                        tool_output=output,
                        timestamp=datetime.now(UTC).isoformat(),
                    )
                )
                if error_code is not None:
                    agent_run.error = f"{error_code.value}: {output.get('error', '')}"

                history.append(
                    ChatMessage(role=MessageRole.USER, content=f"Observation: {output}")
                )
                continue

            # No action and no final answer: treat as the answer.
            agent_run.final_output = text
            state.transition_to(RunStatus.COMPLETED)
            agent_run.status = _legacy_status(state.status)
            break
        else:
            if not RunState.is_terminal(state.status):
                state.transition_to(
                    RunStatus.LIMIT_EXCEEDED,
                    error_code=AgentErrorCode.BUDGET_EXCEEDED,
                    error_detail=f"exceeded maximum steps ({context.budget.max_steps})",
                )
                agent_run.status = _legacy_status(state.status)
                agent_run.error = state.error_detail

        agent_run.finished_at = datetime.now(UTC).isoformat()
        return agent_run

    def _build_system_prompt_from(self, instructions: str, allowed: list[str]) -> str:
        tools = [t for t in self.registry.list_tools() if t.name in allowed]
        tool_descs = [
            f"- {t.name}: {t.description}. Input: {json.dumps(t.input_schema)}"
            for t in tools
        ]
        tools_block = "\n".join(tool_descs) if tool_descs else "No tools available."
        return (
            f"{instructions}\n\n"
            f"Available Tools:\n{tools_block}\n\n"
            "To use a tool, format your output strictly as:\n"
            "Thought: <your reasoning>\n"
            "Action: <tool_name>\n"
            "Action Input: <json_object_with_tool_arguments>\n\n"
            "When you have the final answer, format your output strictly as:\n"
            "Thought: <your reasoning>\n"
            "Final Answer: <your complete response>\n"
        )

    def _build_system_prompt(self, config: AgentConfig) -> str:
        tools = self.registry.list_tools()
        if config.allowed_tools:
            tools = [t for t in tools if t.name in config.allowed_tools]

        tool_descs = [
            f"- {t.name}: {t.description}. Input: {json.dumps(t.input_schema)}"
            for t in tools
        ]
        tools_block = "\n".join(tool_descs) if tool_descs else "No tools available."

        return (
            f"{config.instructions}\n\n"
            f"Available Tools:\n{tools_block}\n\n"
            "To use a tool, format your output strictly as:\n"
            "Thought: <your reasoning>\n"
            "Action: <tool_name>\n"
            "Action Input: <json_object_with_tool_arguments>\n\n"
            "When you have the final answer, format your output strictly as:\n"
            "Thought: <your reasoning>\n"
            "Final Answer: <your complete response>\n"
        )

    async def run(
        self,
        *,
        workspace_id: str,
        goal: str,
        config: AgentConfig | None = None,
    ) -> AgentRun:
        cfg = config or AgentConfig()
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC).isoformat()

        agent_run = AgentRun(
            id=run_id,
            workspace_id=workspace_id,
            goal=goal,
            model=cfg.model,
            status=AgentRunStatus.RUNNING,
            created_at=now,
        )

        system_prompt = self._build_system_prompt(cfg)
        conversation_history: list[ChatMessage] = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=f"Goal: {goal}"),
        ]

        total_tokens = 0

        for step_idx in range(1, cfg.max_steps + 1):
            t_start = time.perf_counter()

            # Call LLM
            req = GenerationRequest(
                model=cfg.model,
                messages=conversation_history,
                temperature=cfg.temperature,
            )
            try:
                gen_res = await self.llm.generate(req)
                t_llm = (time.perf_counter() - t_start) * 1000.0
                step_tokens = (gen_res.prompt_tokens or 0) + (gen_res.completion_tokens or 0)
                total_tokens += step_tokens
                agent_run.total_tokens = total_tokens

                if total_tokens > cfg.budget_tokens:
                    agent_run.status = AgentRunStatus.BUDGET_EXCEEDED
                    agent_run.error = (
                        f"Token budget exceeded ({total_tokens} > {cfg.budget_tokens})"
                    )
                    break

                response_text = gen_res.text.strip()
            except Exception as exc:
                agent_run.status = AgentRunStatus.FAILED
                agent_run.error = f"LLM error: {exc}"
                break

            # Parse ReAct components
            action_match = re.search(r"Action:\s*([a-zA-Z0-9_\-]+)", response_text)
            action_input_match = re.search(
                r"Action Input:\s*(\{.*?\}|\[.*?\]|[^\n]+)", response_text, re.DOTALL
            )
            final_answer_match = re.search(r"Final Answer:\s*(.+)", response_text, re.DOTALL)

            if final_answer_match and not action_match:
                final_answer = final_answer_match.group(1).strip()
                agent_run.steps.append(
                    AgentStep(
                        step_number=step_idx,
                        type=AgentStepType.FINAL_ANSWER,
                        content=response_text,
                        latency_ms=t_llm,
                        timestamp=datetime.now(UTC).isoformat(),
                    )
                )
                agent_run.final_output = final_answer
                agent_run.status = AgentRunStatus.COMPLETED
                break

            if action_match:
                tool_name = action_match.group(1).strip()
                raw_input = action_input_match.group(1).strip() if action_input_match else "{}"

                try:
                    tool_input = json.loads(raw_input)
                except Exception:
                    tool_input = {"input": raw_input}

                agent_run.steps.append(
                    AgentStep(
                        step_number=step_idx,
                        type=AgentStepType.TOOL_CALL,
                        content=response_text,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        latency_ms=t_llm,
                        timestamp=datetime.now(UTC).isoformat(),
                    )
                )

                # Execute Tool
                tool = self.registry.get(tool_name)
                t_tool_start = time.perf_counter()
                if tool:
                    try:
                        tool_output = await tool.execute(workspace_id, **tool_input)
                    except Exception as err:
                        tool_output = {"error": f"Tool execution failed: {err}"}
                else:
                    tool_output = {"error": f"Tool '{tool_name}' not found."}
                t_tool = (time.perf_counter() - t_tool_start) * 1000.0

                agent_run.steps.append(
                    AgentStep(
                        step_number=step_idx,
                        type=AgentStepType.TOOL_OUTPUT,
                        tool_name=tool_name,
                        tool_output=tool_output,
                        latency_ms=t_tool,
                        timestamp=datetime.now(UTC).isoformat(),
                    )
                )

                # Feed observation back to conversation history
                conversation_history.append(
                    ChatMessage(role=MessageRole.ASSISTANT, content=response_text)
                )
                conversation_history.append(
                    ChatMessage(
                        role=MessageRole.USER,
                        content=f"Observation: {json.dumps(tool_output)}",
                    )
                )
            else:
                agent_run.steps.append(
                    AgentStep(
                        step_number=step_idx,
                        type=AgentStepType.FINAL_ANSWER,
                        content=response_text,
                        latency_ms=t_llm,
                        timestamp=datetime.now(UTC).isoformat(),
                    )
                )
                agent_run.final_output = response_text
                agent_run.status = AgentRunStatus.COMPLETED
                break
        else:
            if agent_run.status == AgentRunStatus.RUNNING:
                agent_run.status = AgentRunStatus.MAX_STEPS_EXCEEDED
                agent_run.error = f"Exceeded maximum steps ({cfg.max_steps})"

        agent_run.finished_at = datetime.now(UTC).isoformat()
        return agent_run
