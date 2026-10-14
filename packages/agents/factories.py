"""Factories for V3 agent runtime and components (T-R14).

Wires:
1. LLM provider resolution via LiteLLM/OpenRouter provider seam (ARCH-02 surface).
2. Tool registry populated with CalculatorTool, KnowledgeSearchTool, KnowledgeReadTool.
3. Real AgentRuntime instantiation with server policy and budget checking.
"""

from __future__ import annotations

import os
from typing import Any

from models.interfaces import LLMProvider

from .runtime import AgentRuntime
from .tools import CalculatorTool, KnowledgeReadTool, KnowledgeSearchTool, Tool, ToolRegistry
from .unavailable import MISSING_LLM_PROVIDER, AgentUnavailableError


def resolve_agent_llm_provider(call_site: str = "agents.runtime") -> LLMProvider | None:
    """Return a real configured LLM provider, or None when unconfigured.

    Uses the LiteLLM/OpenRouter provider seam (ARCH-02 surface) with Gemma free-first
    and automatic paid fallback. Returns None if gateway or key is missing.
    """
    base_url = os.environ.get("LITELLM_URL") or os.environ.get("OPENROUTER_API_KEY")
    has_key = any(
        os.environ.get(name)
        for name in ("LITELLM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")
    )
    if not base_url or not has_key:
        return None
    try:
        from models.adapters.litellm_provider import create_llm_provider

        return create_llm_provider(call_site=call_site)
    except Exception:
        return None


def create_agent_runtime(
    llm: LLMProvider | None = None,
    *,
    tools: list[Tool] | None = None,
    orchestrator: Any | None = None,
    chunk_reader: Any | None = None,
    require_llm: bool = True,
    call_site: str = "agents.runtime",
) -> AgentRuntime:
    """Create a configured AgentRuntime with standard tools registered.

    Tools included by default:
    - CalculatorTool (math calculation)
    - KnowledgeSearchTool (ACL-scoped hybrid search)
    - KnowledgeReadTool (ACL-scoped chunk retrieval)

    Raises AgentUnavailableError if require_llm is True and no LLM is configured.
    """
    if llm is None:
        llm = resolve_agent_llm_provider(call_site=call_site)
    if llm is None:
        raise AgentUnavailableError(
            MISSING_LLM_PROVIDER,
            "No LLM provider is configured (no LITELLM_URL and no usable API key); "
            "the run was not executed and no answer was produced.",
        )
    registry = ToolRegistry()
    if tools is not None:
        for t in tools:
            registry.register(t)
    else:
        registry.register(CalculatorTool())
        registry.register(KnowledgeSearchTool(orchestrator=orchestrator))
        registry.register(KnowledgeReadTool(chunk_reader=chunk_reader))

    return AgentRuntime(llm, registry)
