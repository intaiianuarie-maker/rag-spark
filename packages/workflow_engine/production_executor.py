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
        verifier: Any = None,
    ) -> None:
        self._manifest = manifest if manifest is not None else PRODUCTION_CAPABILITY_MANIFEST
        self._llm_provider = llm_provider
        self._verifier = verifier

    @property
    def manifest(self) -> CapabilityManifest:
        return self._manifest

    @property
    def llm_provider(self) -> LLMProvider | None:
        return self._llm_provider

    @property
    def verifier(self) -> Any:
        return self._verifier

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
                verdict = await self._evaluate_verification(node, result_payload["output"])
                # The published output is the covered representation — never
                # the raw model payload (T-RV19-01, POLICE F1).
                published = verdict.pop("output", None)
                result_payload.update(verdict)
                if published is not None:
                    result_payload["output"] = published
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
        """Evaluate the GENERATED output through the Stage 11+12 publication gate.

        The published content is exactly the content that was verified
        (T-RV19-01, POLICE F1):

        - Claims are extracted from the **generated output**. The node's
          preconfigured evidence supplies *references* (chunk ids) only; it is
          never verified in place of the produced text.
        - A structured answer's ``answer`` field is published content, so it
          joins the verification set. A verifier that only supports the
          model's ``claims`` can no longer pass an answer it never saw.
        - Prose outside the JSON payload (prefix/suffix) is uncovered: it is
          rejected, never published.
        - The published output is always the *covered representation* (the
          answer text, the claims' text, or the prose itself). The raw model
          payload is never published, and the ``verified`` flag is the gate
          that says whether the published text passed Stage 11 + Stage 12.

        Only emits ``verified=True`` + ``verification_stage="Stage 11 & 12"``
        when both Stage 11 (deterministic citation integrity) and Stage 12
        (semantic verification) pass over the published content.
        """
        configured_evidence = self._extract_node_evidence(node)
        evidence_missing = configured_evidence is None
        # No evidence configuration means there are no references, so nothing
        # can be verified. The flow continues so the published content is
        # still the covered representation, never the raw payload.
        evidence: dict[str, Any] = configured_evidence or {}

        from answer.citation_integrity import CandidateCitation
        from answer.models import CanonicalChunk as _CanonicalChunk
        from answer.models import Claim as _Claim

        # References come from configured evidence only (T-RV19-01): the
        # claims that get verified are derived from the ACTUAL generated
        # output, never from the node's preconfigured evidence claims. The
        # configured evidence supplies the *reference set* (chunk ids) those
        # output claims are checked against.
        evidence_citations = [
            c if isinstance(c, CandidateCitation) else CandidateCitation(**c)
            for c in (evidence.get("citations") or [])
        ]
        evidence_claims = [
            c if isinstance(c, _Claim) else _Claim(**c)
            for c in (evidence.get("claims") or [])
        ]
        reference_ids: list[str] = []
        for chunk_id in [c.chunk_id for c in evidence_citations] + [
            cid for c in evidence_claims for cid in c.chunk_ids
        ]:
            if chunk_id and chunk_id not in reference_ids:
                reference_ids.append(chunk_id)
        canonical = evidence.get("canonical") or {}
        canonical = {
            k: v if isinstance(v, _CanonicalChunk) else _CanonicalChunk(**v)
            for k, v in canonical.items()
        }
        allowed_workspace_ids = evidence.get("allowed_workspace_ids")
        # Resolve the semantic verifier through the runtime DI boundary first,
        # then the per-node evidence (T-RV19-02).
        verifier = evidence.get("verifier")
        if verifier is None:
            verifier = self._verifier
        chunk_lookup = evidence.get("chunk_lookup")

        # --- Split the generated output into payload + uncovered prose ---
        # (T-RV19-01, POLICE F1)
        payload, residual = self._split_output_payload(output)
        answer_text = ""
        claims: list[_Claim] = []
        if payload is not None:
            answer_text = str(payload.get("answer") or "").strip()
            for entry in payload.get("claims", []) or []:
                if not isinstance(entry, dict):
                    continue
                text = entry.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                chunk_ids = entry.get("chunk_ids", [])
                if not isinstance(chunk_ids, list):
                    chunk_ids = []
                claims.append(
                    _Claim(
                        text=text.strip(),
                        chunk_ids=[str(cid) for cid in chunk_ids],
                    )
                )
        if payload is None and (output or "").strip():
            # Plain-prose output: one claim carrying the generated text.
            claims = [_Claim(text=str(output).strip(), chunk_ids=list(reference_ids))]

        # A claim that asserted no reference of its own inherits the
        # configured reference set; claims that did assert references keep
        # them (they must exist in the canonical store to pass).
        for claim in claims:
            if not claim.chunk_ids:
                claim.chunk_ids = list(reference_ids)

        # The structured answer IS published content, so it is verified too.
        # Without this a verifier that only supports the model's claims could
        # pass an answer it never saw (POLICE F1).
        if answer_text and not self._text_covered(answer_text, claims):
            claims.append(_Claim(text=answer_text, chunk_ids=list(reference_ids)))

        # The published content is the covered representation: the answer
        # text, the claims' text, or the prose itself. The raw model payload
        # is never published.
        covered_output = self._covered_representation(answer_text, claims)

        # Uncovered prose around the payload (prefix/suffix) is rejected: it
        # is content no claim covers and must never be published.
        if residual:
            return {
                "verified": False,
                "verification_stage": "uncovered_output",
                "verification_reasons": ["uncovered_output_text"],
                "output": covered_output,
            }

        if not claims:
            return {
                "verified": False,
                "verification_stage": "insufficient_evidence",
                "verification_reasons": ["no_claims_extracted_from_output"],
                "output": "",
            }

        if not any(c.chunk_ids for c in claims):
            return {
                "verified": False,
                "verification_stage": "insufficient_evidence",
                "verification_reasons": (
                    ["no_evidence_provided"] if evidence_missing else ["no_references_provided"]
                ),
                "output": covered_output,
            }

        # Bind the reference set to the *output* claim texts: the evidence
        # citation provenance (page/section/span) is preserved, but the
        # candidate citation is attributed to the text actually verified.
        citations: list[CandidateCitation] = []
        for claim in claims:
            for chunk_id in claim.chunk_ids:
                base = next(
                    (c for c in evidence_citations if c.chunk_id == chunk_id), None
                )
                if base is not None:
                    citations.append(base.model_copy(update={"claim_text": claim.text}))
                else:
                    citations.append(
                        CandidateCitation(chunk_id=chunk_id, claim_text=claim.text)
                    )

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
                "output": covered_output,
            }

        # --- Stage 12: semantic verification ---
        # T-RV19-02: never claim Stage 12 without a successful Stage 12
        # report. If no verifier is available, return an explicit
        # incomplete/unavailable result instead of falling through to success.
        if verifier is None:
            return {
                "verified": False,
                "verification_stage": "Stage 12 unavailable",
                "verification_reasons": ["no_verifier_configured"],
                "output": covered_output,
            }

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
                "output": covered_output,
            }

        # The gate passed over the published content: publish exactly the
        # covered representation, never the raw model payload (POLICE F1).
        return {
            "verified": True,
            "verification_stage": "Stage 11 & 12",
            "output": covered_output,
        }

    @staticmethod
    def _split_output_payload(output: str) -> tuple[dict[str, Any] | None, str]:
        """Split the generated output into a JSON payload and uncovered prose.

        Returns ``(payload, residual)``:

        - ``payload`` is the parsed JSON object of the model output (fenced or
          embedded in prose), or ``None`` when the output is plain prose.
        - ``residual`` is the text OUTSIDE that JSON block (prefix/suffix
          prose). Any non-empty residual is content that no claim covers, so
          it must never be published (T-RV19-01, POLICE F1).

        The block is located with a brace-depth scan that ignores braces
        inside strings, so a JSON string containing ``}`` cannot truncate the
        payload. Parsing of the located block is delegated to
        ``answer.prompts.parse_model_output`` so the workflow executor and
        the answer engine agree on the answer/claims shape.
        """
        from answer.prompts import parse_model_output

        stripped = (output or "").strip()
        if not stripped:
            return None, ""
        candidate = stripped
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if len(lines) >= 3:
                candidate = "\n".join(lines[1:-1]).strip()
        start = candidate.find("{")
        if start == -1:
            return None, ""
        depth = 0
        end = -1
        in_string = False
        escaped = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1:
            return None, ""
        block = candidate[start : end + 1]
        residual = (candidate[:start] + candidate[end + 1 :]).replace("`", " ").strip()
        try:
            parsed = parse_model_output(block)
        except ValueError:
            return None, ""
        return parsed, residual

    @staticmethod
    def _text_covered(text: str, claims: list[Any]) -> bool:
        """Return True when ``text`` is already one of the verified claims."""
        normalized = " ".join(text.split()).casefold()
        if not normalized:
            return False
        for claim in claims:
            claim_text = " ".join(str(getattr(claim, "text", "")).split()).casefold()
            if claim_text and claim_text == normalized:
                return True
        return False

    @staticmethod
    def _covered_representation(answer_text: str, claims: list[Any]) -> str:
        """Return the published text: the covered representation of the output.

        For a structured answer this is the answer text (the content that is
        published); for a claims-only payload it is the claims' text joined
        by newlines; for plain prose it is the prose itself (a single claim
        covering it). The raw model payload is never published.
        """
        if answer_text:
            return answer_text
        texts = [
            str(getattr(claim, "text", "")).strip()
            for claim in claims
            if str(getattr(claim, "text", "")).strip()
        ]
        if texts:
            return "\n".join(texts)
        return ""

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
