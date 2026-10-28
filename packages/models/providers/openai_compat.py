"""Public provider import; HTTP implementation stays at the adapter boundary."""

from models.adapters.openai_compat import OpenAICompatProvider

__all__ = ["OpenAICompatProvider"]
