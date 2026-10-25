"""Compatibility API; concrete SDK implementation lives behind adapters."""

import sys

from .adapters import openrouter_provider as _implementation
from .adapters.openrouter_provider import (
    NEMOTRON_RERANK_MODEL as NEMOTRON_RERANK_MODEL,
)
from .adapters.openrouter_provider import (
    OPENROUTER_RERANK_URL as OPENROUTER_RERANK_URL,
)
from .adapters.openrouter_provider import (
    CohereRerankProvider as CohereRerankProvider,
)
from .adapters.openrouter_provider import (
    NemotronRerankProvider as NemotronRerankProvider,
)
from .adapters.openrouter_provider import (
    OpenRouterReranker as OpenRouterReranker,
)
from .adapters.openrouter_provider import (
    RerankAvailabilityError as RerankAvailabilityError,
)
from .adapters.openrouter_provider import (
    RerankItem as RerankItem,
)
from .adapters.openrouter_provider import (
    RerankResult as RerankResult,
)
from .adapters.openrouter_provider import (
    _finite_float as _finite_float,
)
from .adapters.openrouter_provider import (
    _normalize_scores as _normalize_scores,
)

# Preserve module identity for dependency injection and existing callers.
sys.modules[__name__] = _implementation
