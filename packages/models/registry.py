"""Model/provider registry (TASK-M7-001a).

In-memory registry of LLM/Embedding providers served over OpenAI-compatible
HTTP endpoints, with capability metadata. No vendor SDKs are imported here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProviderKind(StrEnum):
    LLM = "llm"
    EMBEDDING = "embedding"
    ANY = "any"


class UnknownModelError(KeyError):
    """Raised when a model cannot be resolved in the registry."""


class UnknownModel(UnknownModelError):
    """Alias for compatibility."""


@dataclass(frozen=True)
class Capability:
    """Capability flags for a model/provider."""

    chat: bool = False
    reasoning: bool = False
    coding: bool = False
    multilingual: bool = False
    long_context: bool = False
    tags: tuple[str, ...] = ()

    def supports(self, name: str) -> bool:
        if hasattr(self, name) and isinstance(getattr(self, name), bool):
            return getattr(self, name)
        return name in self.tags

    def names(self) -> frozenset[str]:
        out = {
            n
            for n in ("chat", "reasoning", "coding", "multilingual", "long_context")
            if getattr(self, n)
        }
        out |= set(self.tags)
        return frozenset(out)


@dataclass
class ProviderSpec:
    name: str
    kind: ProviderKind = ProviderKind.ANY
    base_url: str = ""
    model: str = ""
    capabilities: Capability | frozenset[str] | tuple[str, ...] | set[str] = field(
        default_factory=frozenset
    )
    cost: str = "low"
    latency: str = "medium"
    privacy_class: str = "cloud"
    local: bool = False
    api_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def capability_names(self) -> frozenset[str]:
        caps = self.capabilities
        if isinstance(caps, Capability):
            return caps.names()
        return frozenset(caps or ())


class ModelRegistry:
    """Registry mapping model names to providers + capability metadata."""

    def __init__(self) -> None:
        self._entries: dict[str, Any] = {}  # model -> provider instance or spec
        self._specs: dict[str, ProviderSpec] = {}

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def register(
        self,
        model: str,
        provider: Any = None,
        *,
        name: str | None = None,
        base_url: str | None = None,
        kind: ProviderKind | str | None = None,
        capabilities: Any = None,
        cost: str = "low",
        latency: str = "medium",
        privacy_class: str = "cloud",
        local: bool = False,
        api_key: str | None = None,
        spec: ProviderSpec | None = None,
        **extra: Any,
    ) -> Any:
        if spec is not None:
            provider = spec
        if provider is None:
            from .providers.openai_compat import OpenAICompatProvider

            k = ProviderKind(kind) if kind is not None else ProviderKind.LLM
            caps = capabilities if capabilities is not None else frozenset()
            provider = OpenAICompatProvider(
                name=name or model,
                base_url=base_url or "",
                model=model,
                kind=k,
                capabilities=caps,
                cost=cost,
                latency=latency,
                privacy_class=privacy_class,
                local=local,
                api_key=api_key,
                extra=extra,
            )
        else:
            # Attach a spec if the provider lacks one.
            if not hasattr(provider, "spec"):
                caps = (
                    capabilities
                    if capabilities is not None
                    else getattr(provider, "capabilities", frozenset())
                )
                kind_val = kind if kind is not None else getattr(provider, "kind", ProviderKind.ANY)
                try:
                    kind_val = ProviderKind(kind_val)
                except (ValueError, TypeError):
                    kind_val = ProviderKind.ANY
                provider.spec = ProviderSpec(
                    name=str(name or getattr(provider, "name", model)),
                    kind=kind_val,
                    base_url=str(base_url or getattr(provider, "base_url", "")),
                    model=getattr(provider, "model", model),
                    capabilities=caps if caps is not None else frozenset(),
                    cost=cost if cost != "low" else getattr(provider, "cost", cost),
                    latency=latency
                    if latency != "medium"
                    else getattr(provider, "latency", latency),
                    privacy_class=privacy_class
                    if privacy_class != "cloud"
                    else getattr(provider, "privacy_class", privacy_class),
                    local=local or getattr(provider, "local", False),
                    api_key=api_key if api_key is not None else getattr(provider, "api_key", None),
                    extra=extra or getattr(provider, "extra", {}),
                )
            elif capabilities is not None and hasattr(provider, "spec"):
                # allow capability override
                try:
                    provider.spec.capabilities = capabilities
                except Exception:
                    pass
            # attach capabilities convenience
            if capabilities is not None and not hasattr(provider, "capabilities"):
                provider.capabilities = capabilities
            if not hasattr(provider, "capabilities"):
                provider.capabilities = (
                    getattr(provider, "spec", None) and provider.spec.capabilities
                )
        # normalize capabilities attribute
        if hasattr(provider, "spec") and not getattr(provider, "capabilities", None):
            provider.capabilities = provider.spec.capabilities
        self._entries[model] = provider
        if hasattr(provider, "spec"):
            self._specs[model] = provider.spec
        else:
            self._specs[model] = ProviderSpec(
                name=name or model,
                kind=ProviderKind.ANY,
                model=model,
                capabilities=capabilities or frozenset(),
            )
        return provider

    def register_provider(self, provider: Any, *, models: Iterable[str] | None = None) -> None:
        models = list(models) if models is not None else [str(getattr(provider, "model", ""))]
        models = [str(m) for m in models if m]
        for m in models:
            self.register(m, provider)

    def unregister(self, model: str) -> None:
        self._entries.pop(model, None)
        self._specs.pop(model, None)

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #
    def resolve(self, model: str) -> Any:
        if model not in self._entries:
            raise UnknownModelError(f"unknown model: {model!r}")
        return self._entries[model]

    def get(self, model: str) -> Any:
        return self.resolve(model)

    def __contains__(self, model: str) -> bool:
        return model in self._entries

    def __getitem__(self, model: str) -> Any:
        return self.resolve(model)

    # ------------------------------------------------------------------ #
    # Capability query
    # ------------------------------------------------------------------ #
    def capability_query(self, capability: str) -> list[str]:
        out: list[str] = []
        for model, provider in self._entries.items():
            caps = self._capabilities_of(provider, model)
            if capability in caps:
                out.append(model)
        return out

    def models_with_capability(self, capability: str) -> list[str]:
        return self.capability_query(capability)

    def supports(self, model: str, capability: str) -> bool:
        provider = self.resolve(model)
        return capability in self._capabilities_of(provider, model)

    def _capabilities_of(self, provider: Any, model: str) -> frozenset[str]:
        caps = getattr(provider, "capabilities", None)
        if isinstance(caps, Capability):
            return caps.names()
        if isinstance(caps, (frozenset, set, tuple, list)):
            # could be strings or Capability
            names: set[str] = set()
            for c in caps:
                if isinstance(c, Capability):
                    names |= c.names()
                else:
                    names.add(str(c))
            return frozenset(names)
        spec = getattr(provider, "spec", None) or self._specs.get(model)
        if spec is not None:
            return spec.capability_names()
        return frozenset()

    # ------------------------------------------------------------------ #
    # Listing
    # ------------------------------------------------------------------ #
    def list_models(self, kind: ProviderKind | str | None = None) -> list[str]:
        if kind is None:
            return list(self._entries.keys())
        try:
            k = ProviderKind(kind)
        except (ValueError, TypeError):
            return list(self._entries.keys())
        out = []
        for model, provider in self._entries.items():
            spec = getattr(provider, "spec", None) or self._specs.get(model)
            if spec is not None and spec.kind == k:
                out.append(model)
            elif k == ProviderKind.ANY:
                out.append(model)
        return out

    def models(self) -> list[str]:
        return list(self._entries.keys())

    def specs(self) -> dict[str, ProviderSpec]:
        return dict(self._specs)

    def clear(self) -> None:
        self._entries.clear()
        self._specs.clear()


# Module-level default registry + convenience functions.
registry = ModelRegistry()
default_registry = registry


def register(*args: Any, **kwargs: Any) -> Any:
    # Support register(model, provider) and register(model, provider=...)
    if len(args) >= 2 and "model" not in kwargs:
        model = args[0]
        provider = args[1]
        kwargs = dict(kwargs)
        kwargs["provider"] = provider
        return registry.register(model, **kwargs)
    if len(args) == 1:
        return registry.register(args[0], **kwargs)
    return registry.register(**kwargs)


def resolve(model: str) -> Any:
    return registry.resolve(model)


def capability_query(capability: str) -> list[str]:
    return registry.capability_query(capability)


# Aliases
ProviderRegistry = ModelRegistry
Registry = ModelRegistry
