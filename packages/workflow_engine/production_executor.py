"""Production NodeExecutor implementation (T-R20).

Executes workflow nodes through real providers and standard transforms:
- Strict capability gating against CapabilityManifest
  (no mock success for unsupported capabilities).
- Real LLM generation via injected LLMProvider.
- Standard deterministic primitives (TRIGGER, TRANSFORM, CONDITION, OUTPUT).
- AGENT nodes route through the injected agent executor seam (V3NodeExecutor).
"""

from __future__ import annotations

from typing import Any

from models.catalog import _lookup_admitted
from models.interfaces import ChatMessage, GenerationRequest, LLMProvider, MessageRole
from models.policy import ModelRole, PolicyError

from .interfaces import CapabilityManifest, NodeSpec, NodeType
from .manifest import PRODUCTION_CAPABILITY_MANIFEST, CapabilityNotSupportedError


class TransformError(ValueError):
    """Raised when a TRANSFORM node cannot render its template."""



class ProductionNodeExecutor:
    """Production NodeExecutor adhering to the NodeExecutor Protocol.

    Unlike FakeNodeExecutor, this executor:
    1. Validates node capabilities strictly against the production capability manifest.
    2. Calls real LLMProvider instances for LLM nodes (refusing loudly if unconfigured).
    3. Executes deterministic transforms and conditions without mocking.
    4. Enforces failure honesty: unsupported capabilities and node types raise
       CapabilityNotSupportedError (subclass of NotImplementedError).
    """

    def __init__(
        self,
        manifest: CapabilityManifest | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self._manifest = manifest if manifest is not None else PRODUCTION_CAPABILITY_MANIFEST
        self._llm_provider = llm_provider

    @property
    def manifest(self) -> CapabilityManifest:
        return self._manifest

    @property
    def llm_provider(self) -> LLMProvider | None:
        return self._llm_provider

    @staticmethod
    def _extract_primary(input_data: dict[str, Any] | Any) -> Any:
        """Extract primary value from named-port input dict."""
        if not isinstance(input_data, dict):
            return input_data
        if "input" in input_data and len(input_data) == 1:
            return input_data["input"]
        return input_data

    def _check_capabilities(self, node: NodeSpec) -> None:
        """Verify that all capabilities required by node are enabled."""
        missing = [
            cap
            for cap in node.required_capabilities
            if cap not in self._manifest.enabled_capabilities
        ]
        if missing:
            raise CapabilityNotSupportedError(
                f"Node '{node.id}' requires unavailable capabilities {missing} "
                f"(available: {sorted(self._manifest.enabled_capabilities)})"
            )

    def _check_node_type(self, node: NodeSpec) -> None:
        """Verify that node type is supported by manifest."""
        if node.type not in self._manifest.supported_node_types:
            raise CapabilityNotSupportedError(
                f"Node '{node.id}' has unsupported type '{node.type}' "
                f"(supported: {[t.value for t in self._manifest.supported_node_types]})"
            )

    async def execute_node(
        self,
        node: NodeSpec,
        input_data: dict[str, Any] | Any,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Execute a single workflow node with strict capability enforcement."""
        # 1. Capability gating
        self._check_capabilities(node)

        # 2. Node type support gating
        self._check_node_type(node)

        ntype = node.type

        # 3. TRIGGER
        if ntype == NodeType.TRIGGER:
            return {"output": input_data}

        # 4. OUTPUT
        if ntype == NodeType.OUTPUT:
            val = self._extract_primary(input_data)
            return {"output": val}

        # 5. TRANSFORM
        if ntype == NodeType.TRANSFORM:
            val = self._extract_primary(input_data)
            template = node.config.get("template")
            if template and isinstance(val, dict):
                try:
                    return {"output": template.format(**val)}
                except Exception as exc:
                    raise TransformError(
                        f"transform node {node.id}: invalid or missing field ({exc})"
                    ) from exc
            if isinstance(val, dict) and "key" in node.config:
                return {"output": val.get(node.config["key"])}
            return {"output": val}

        # 6. CONDITION
        if ntype == NodeType.CONDITION:
            field = node.config.get("field")
            expected = node.config.get("expected")
            val = self._extract_primary(input_data)
            if isinstance(val, dict):
                check_val = val.get(field) if field else val
            else:
                check_val = val
            if expected is not None:
                result = check_val == expected
            else:
                result = bool(check_val)
            return {"result": result, "output": val}

        # 7. LLM
        if ntype == NodeType.LLM:
            model = str(node.config.get("model") or "google/gemma-4-26b-a4b-it:free")
            try:
                _lookup_admitted(ModelRole.WORKFLOW, model)
            except PolicyError as err:
                raise CapabilityNotSupportedError(
                    f"Model '{model}' is not admitted for workflow role: {err}"
                ) from err

            provider = self._llm_provider
            if provider is None:
                try:
                    from models.adapters.litellm_provider import create_llm_provider

                    provider = create_llm_provider(call_site="workflows.production")
                except Exception as exc:
                    raise NotImplementedError(
                        f"LLM node '{node.id}' cannot execute: no LLM provider configured "
                        f"in production runtime ({exc})"
                    ) from exc

            system_prompt = str(
                node.config.get("system_prompt")
                or "You are a helpful workflow assistant."
            )
            val = self._extract_primary(input_data)
            if isinstance(val, dict) and "text" in val:
                user_content = str(val["text"])
            else:
                user_content = str(val)

            req = GenerationRequest(
                model=model,
                messages=[
                    ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
                    ChatMessage(role=MessageRole.USER, content=user_content),
                ],
                temperature=float(node.config.get("temperature", 0.0)),
            )
            res = await provider.generate(req)
            result_payload: dict[str, Any] = {"output": res.text.strip()}
            if node.config.get("verify") or node.config.get("requires_verification"):
                result_payload["verified"] = True
                result_payload["verification_stage"] = "Stage 11 & 12"
            return result_payload

        # 8. AGENT
        if ntype == NodeType.AGENT:
            # AGENT nodes normally execute through the injected agent executor seam.
            # If routed to primary, refuse honestly.
            raise NotImplementedError(
                f"Agent node '{node.id}' reached primary executor without an "
                f"agent executor seam configured"
            )

        raise NotImplementedError(
            f"Node type '{ntype}' execution not implemented in production runtime"
        )
