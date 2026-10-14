"""Extraction-mode dispatch: a declared mode selects its documented extractor.

T-R12. The defect this closes: the graph job constructed
``RuleBasedGraphExtractor()`` unconditionally, whatever ``GRAPH_MODE`` said.
So ``GRAPH_MODE=llm`` ran the deterministic rule extractor and reported success,
and any operator reading the logs or the graph rows had no way to tell. A mode
is a claim about how a thing is built; silently ignoring it makes the claim
false while everything appears to work.

Two implementations already existed and are reused unchanged:

  ``rule`` -> :class:`~graph.extractor.RuleBasedGraphExtractor` — deterministic
      regex NER plus relation cues. No model weights, no network.
  ``llm``  -> :class:`~graph.adapters.lightrag_extractor.LLMBasedGraphExtractor`
      — the LightRAG-pattern LLM extractor, which takes an injected
      ``LLMProvider`` rather than a vendor SDK, so the model router stays the
      only way to reach a model.

Nothing here falls back. If ``llm`` is declared and no provider can be built,
that raises — it does not quietly degrade to the rule extractor, because that
degradation is the exact bug in a different costume. An unrecognised mode is
refused with the mode named, rather than being treated as ``off`` or as
``rule``.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from graph.extractor import GraphExtractor, RuleBasedGraphExtractor
from graph.factory import graph_mode

#: Modes that declare the graph leg ON. Mirrors factory.GRAPH_MODES_ON; kept
#: separate from factory's retrieval concern (the retriever is built for both).
EXTRACTION_MODES: tuple[str, ...] = ("rule", "llm")

#: Human-readable name of the implementation each mode is documented to select.
#: Used by the mode-matrix test and by the refusal message, so the mapping is
#: stated once and cannot drift between the table, the code and the error.
MODE_IMPLEMENTATIONS: dict[str, str] = {
    "rule": "graph.extractor.RuleBasedGraphExtractor",
    "llm": "graph.adapters.lightrag_extractor.LLMBasedGraphExtractor",
}

#: Model used by ``llm`` mode. Overridable per deployment; defaults to the same
#: answer model the rest of the system uses so there is one obvious knob.
DEFAULT_LLM_MODEL = "nvidia/nemotron-3.5-lightning:free"
#: Env keys consulted for the llm-mode model name, in order.
LLM_MODEL_ENV_KEYS: tuple[str, ...] = ("GRAPH_LLM_MODEL", "ANSWER_MODEL")


class GraphModeUnsupported(ValueError):
    """A GRAPH_MODE this dispatcher does not implement.

    A ``ValueError`` because it is a bad configuration value. The mode is
    always named in the message: "unsupported mode" alone would leave the
    operator guessing which string to fix.
    """


def normalize_mode(mode: str | None = None, env: dict[str, str] | None = None) -> str:
    """Normalise a declared mode. ``None`` means "read GRAPH_MODE"."""
    if mode is not None:
        return mode.strip().lower()
    return graph_mode(env)


def describe_mode(mode: str | None = None, env: dict[str, str] | None = None) -> str:
    """Return the documented implementation for a mode.

    Pure and lookup-only — it constructs nothing and needs no provider — so the
    mode matrix can be asserted for every mode without standing up a model.
    """
    normalized = normalize_mode(mode, env)
    if normalized not in MODE_IMPLEMENTATIONS:
        raise GraphModeUnsupported(
            f"unsupported GRAPH_MODE {normalized!r}; supported extraction "
            f"modes are {sorted(MODE_IMPLEMENTATIONS)}"
        )
    return MODE_IMPLEMENTATIONS[normalized]


def llm_model_name(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    for key in LLM_MODEL_ENV_KEYS:
        value = source.get(key)
        if value:
            return value
    return DEFAULT_LLM_MODEL


def build_llm_provider(env: dict[str, str] | None = None) -> object:
    """Build the injected LLM provider for ``llm`` mode via the model router.

    Raises rather than returning ``None``: a caller must never receive a
    half-built llm-mode extractor, and must certainly not receive the rule one.
    """
    source = os.environ if env is None else env
    has_key = any(
        source.get(key)
        for key in ("LITELLM_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")
    )
    if not has_key:
        raise GraphModeUnsupported(
            "GRAPH_MODE=llm requires an LLM provider, but no LITELLM_API_KEY / "
            "OPENROUTER_API_KEY / DEEPSEEK_API_KEY is configured; refusing to "
            "silently run the rule extractor instead"
        )
    from models.adapters.litellm_provider import create_llm_provider

    return create_llm_provider()


def create_graph_extractor(
    mode: str | None = None,
    *,
    env: dict[str, str] | None = None,
    provider: object | None = None,
    model: str | None = None,
) -> GraphExtractor:
    """Construct the extractor the declared mode documents.

    ``provider`` is injectable so tests can drive ``llm`` mode with a fake and
    without a gateway; production passes nothing and gets the router-built one.
    """
    normalized = normalize_mode(mode, env)
    builders: dict[str, Callable[[], GraphExtractor]] = {
        "rule": RuleBasedGraphExtractor,
        "llm": lambda: _build_llm_extractor(env=env, provider=provider, model=model),
    }
    try:
        builder = builders[normalized]
    except KeyError:
        raise GraphModeUnsupported(
            f"unsupported GRAPH_MODE {normalized!r}; supported extraction "
            f"modes are {sorted(MODE_IMPLEMENTATIONS)}"
        ) from None
    return builder()


def _build_llm_extractor(
    *,
    env: dict[str, str] | None,
    provider: object | None,
    model: str | None,
) -> GraphExtractor:
    from graph.adapters import LLMBasedGraphExtractor

    resolved_provider = provider if provider is not None else build_llm_provider(env)
    resolved_model = model or llm_model_name(env)
    return LLMBasedGraphExtractor(provider=resolved_provider, model=resolved_model)  # type: ignore[arg-type]