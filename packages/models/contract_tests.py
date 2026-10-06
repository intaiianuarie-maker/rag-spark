"""Contract checks for the model interface (`models.interfaces.LLMProvider`).

Test-only (TASK-M0-004). No weights, no API keys, no network: pass any
`LLMProvider` implementation (the M0 harness passes an echo fake; M7 passes the
OpenAI-compatible/vLLM/Ollama adapters). The contract is deliberately about the
*seam* — model-name passthrough for routing, and a stream that yields text —
because citations are resolved by our API from evidence ids, never by the model.
"""

from __future__ import annotations

from collections.abc import Awaitable

from .interfaces import ChatMessage, GenerationRequest, GenerationResult, LLMProvider, MessageRole

CONTRACT_MODEL = "contract-model"


def _fail(message: str) -> None:
    raise AssertionError(message)


def _require_instance(value: object, expected_type: type, what: str) -> None:
    if not isinstance(value, expected_type):
        _fail(f"{what}: expected {expected_type.__name__}, got {type(value).__name__}")


async def _expect_error(awaitable: Awaitable[object], what: str) -> None:
    """Invalid input must raise loudly (no silent empty completion)."""
    try:
        result = await awaitable
    except Exception:  # any loud failure satisfies the contract
        return
    _fail(f"{what}: expected an exception, but returned {result!r}")


def _ping_request(model: str = CONTRACT_MODEL) -> GenerationRequest:
    return GenerationRequest(
        model=model,
        messages=[ChatMessage(role=MessageRole.USER, content="ping")],
        temperature=0.0,
    )


async def check_llm_provider_contract(provider: LLMProvider) -> None:
    """generate/stream call shape + model-name passthrough."""
    if not provider.provider_name:
        _fail("LLMProvider.provider_name must be set (routing/observability contract)")

    result = await provider.generate(_ping_request())
    _require_instance(result, GenerationResult, "generate return value")
    if result.model != CONTRACT_MODEL:
        _fail(f"generate must pass the requested model through, got {result.model!r}")
    if not result.text:
        _fail("generate returned no text for a non-empty prompt")

    chunks = [chunk async for chunk in provider.stream(_ping_request())]
    if not chunks:
        _fail("stream yielded no chunks for a non-empty prompt")
    if not "".join(chunks).strip():
        _fail("stream yielded only whitespace")

    await _expect_error(
        provider.generate(_ping_request(model="")),
        "generate with an empty model name",
    )
