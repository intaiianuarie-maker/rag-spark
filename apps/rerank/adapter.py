"""Compatibility API; concrete SDK implementation lives behind adapters."""

import sys

from .adapters import minilm_http as _implementation
from .adapters.minilm_http import (
    DEFAULT_BASE_URL as DEFAULT_BASE_URL,
)
from .adapters.minilm_http import (
    DEFAULT_MODEL as DEFAULT_MODEL,
)
from .adapters.minilm_http import (
    DEFAULT_TIMEOUT_SECONDS as DEFAULT_TIMEOUT_SECONDS,
)
from .adapters.minilm_http import (
    MiniLMReranker as MiniLMReranker,
)
from .adapters.minilm_http import (
    MiniLMRerankerError as MiniLMRerankerError,
)
from .adapters.minilm_http import (
    logger as logger,
)

# Preserve module identity for dependency injection and existing callers.
sys.modules[__name__] = _implementation
