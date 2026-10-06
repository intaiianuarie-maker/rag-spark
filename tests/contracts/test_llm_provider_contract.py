"""LLMProvider contract (`models.interfaces.LLMProvider`).

Echo fake only: no model weights, no API keys (TASK-M0-004 acceptance). M7's
OpenAI-compatible/vLLM/Ollama adapters run this same
`check_llm_provider_contract` in their own task; here we lock the seam —
`provider_name`, model-name passthrough for routing, and `stream` yielding text.
"""

from __future__ import annotations

import pytest
from harness_fakes import FakeLLMProvider
from models.contract_tests import check_llm_provider_contract
from models.interfaces import ChatMessage, GenerationRequest, MessageRole

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def _request(content: str) -> GenerationRequest:
    return GenerationRequest(
        model="contract-model",
        messages=[ChatMessage(role=MessageRole.USER, content=content)],
    )


async def test_fake_llm_provider_satisfies_contract() -> None:
    await check_llm_provider_contract(FakeLLMProvider())


async def test_stream_chunks_concatenate_to_the_generated_completion() -> None:
    provider = FakeLLMProvider()
    request = _request("alpha beta gamma")
    chunks = [chunk async for chunk in provider.stream(request)]
    assert chunks and all(chunk.strip() for chunk in chunks)
    assert "".join(chunks) == (await provider.generate(request)).text
