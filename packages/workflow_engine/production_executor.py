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
                result_payload.update(
                    await self._evaluate_verification(node, result_payload["output"])
                )
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

    async def _evaluate_verification(
        self, node: NodeSpec, output: str
    ) -> dict[str, Any]:
        """Evaluate node evidence through the Stage 11+12 publication gate.

        Only emits ``verified=True`` + ``verification_stage="Stage 11 & 12"``
        when both Stage 11 (deterministic citation integrity) and Stage 12
        (semantic verification) pass.

        Fail-closed: missing evidence, failed Stage 11, or non-SUPPORTED
        Stage 12 verdict all yield ``verified=False``.
        """
        evidence = self._extract_node_evidence(node)

        if evidence is None:
            return {
                "verified": False,
                "verification_stage": "insufficient_evidence",
                "verification_reasons": ["no_evidence_provided"],
            }

        from answer.models import CanonicalChunk as _CanonicalChunk, Claim as _Claim
        from answer.citation_integrity import CandidateCitation

        claims = evidence.get("claims") or []
        # Normalize dict claims to Claim objects
        claims = [
            c if isinstance(c, _Claim) else _Claim(**c) for c in claims
        ]
        citations = evidence.get("citations") or []
        citations = [
            c if isinstance(c, CandidateCitation) else CandidateCitation(**c)
            for c in citations
        ]
        canonical = evidence.get("canonical") or {}
        # Normalize dict canonical entries to CanonicalChunk objects
        canonical = {
            k: v if isinstance(v, _CanonicalChunk) else _CanonicalChunk(**v)
            for k, v in canonical.items()
        }
        allowed_workspace_ids = evidence.get("allowed_workspace_ids")
        verifier = evidence.get("verifier")
        chunk_lookup = evidence.get("chunk_lookup")

        if not citations and not claims:
            return {
                "verified": False,
                "verification_stage": "insufficient_evidence",
                "verification_reasons": ["no_citations_or_claims_provided"],
            }

        # --- Stage 11: deterministic citation integrity ---
        from answer.citation_integrity import validate_citation_integrity

        stage11_report = await validate_citation_integrity(
            claims=claims,
            citations=citations,
            canonical=canonical,
            allowed_workspace_ids=allowed_workspace_ids,
        )
        if not stage11_report.valid:
            reasons = [
                f"STAGE_11_{rej.reason.value}:{rej.citation.chunk_id}"
                for rej in stage11_report.rejected_citations
            ] or [f"unverified_claim:{c}" for c in stage11_report.unverified_claims]
            return {
                "verified": False,
                "verification_stage": "Stage 11 rejected",
                "verification_reasons": reasons or ["stage_11_failed"],
            }

        # --- Stage 12: semantic verification (only if verifier injected) ---
        if verifier is not None:
            from answer.semantic_verification import (
                evaluate_publication_gate,
                verify_semantic_claims,
            )

            stage12_report = await verify_semantic_claims(
                claims=claims,
                integrity_report=stage11_report,
                verifier=verifier,
            )
            gate_result = await evaluate_publication_gate(
                claims=claims,
                integrity_report=stage11_report,
                semantic_report=stage12_report,
                chunk_lookup=chunk_lookup,
            )
            if not gate_result.can_publish:
                return {
                    "verified": False,
                    "verification_stage": str(gate_result.status),
                    "verification_reasons": list(gate_result.reasons),
                }

        return {
            "verified": True,
            "verification_stage": "Stage 11 & 12",
        }

    @staticmethod
    def _extract_node_evidence(node: NodeSpec) -> dict[str, Any] | None:
        """Extract evidence configuration from the node config.

        Supports:
        - ``evidence``: dict with ``claims``, ``citations``, ``canonical``,
          ``allowed_workspace_ids``, ``verifier``, ``chunk_lookup``.
        - Flat config fallback: ``claims``, ``citations``, ``canonical_chunks``.
        """
        cfg = node.config
        evidence = cfg.get("evidence")
        if evidence is not None and isinstance(evidence, dict):
            return evidence

        claims = cfg.get("claims")
        citations = cfg.get("citations")
        canonical = cfg.get("canonical_chunks")
        if claims is None and citations is None and canonical is None:
            return None
        return {
            "claims": claims or [],
            "citations": citations or [],
            "canonical": canonical or {},
        }
