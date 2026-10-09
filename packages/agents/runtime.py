"""Agent Runtime execution engine (Section 25).

Executes the ReAct loop: Goal -> Plan -> Tool Call -> Observation -> Final Answer.
Maintains a durable step trace on the AgentRun object for replay and debugging.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import UTC, datetime

from models.interfaces import ChatMessage, GenerationRequest, LLMProvider, MessageRole

from .interfaces import AgentConfig, AgentRun, AgentRunStatus, AgentStep, AgentStepType
from .tools import ToolRegistry


class AgentRuntime:
    """Executes multi-step agent workflows with tools and audit tracing."""

    def __init__(self, llm_provider: LLMProvider, tool_registry: ToolRegistry) -> None:
        self.llm = llm_provider
        self.registry = tool_registry

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
