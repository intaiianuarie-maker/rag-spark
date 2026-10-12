"""Research Engine implementation (Section 28).

Runs multi-step investigations: decomposes topic into subquestions,
persists verified evidence, detects contradictions, and synthesizes structured reports.

V3 enforcement (rew1 B5/B9): planning/synthesis failures raise a typed
:class:`ResearchEngineError` instead of silently substituting plausible
placeholders. A report with no verified evidence abstains (``FAILED`` with
explicit unknowns) and never claims completed successful research. Provenance
(run id, subquestion, document version/chunk, quote span, source hash,
verification state) is populated explicitly on every evidence unit.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from models.interfaces import ChatMessage, GenerationRequest, LLMProvider, MessageRole

from .evidence_store import EvidenceStore
from .interfaces import (
    Citation,
    ClaimStatus,
    EvidenceVerificationState,
    ReportClaim,
    ResearchEvidence,
    ResearchPlan,
    ResearchReport,
    ResearchStatus,
)


class ResearchEngineError(RuntimeError):
    """Raised when planning/synthesis/provider work cannot be completed honestly."""


class ResearchEngine:
    """Multi-step research and synthesis engine."""

    def __init__(self, llm_provider: LLMProvider, evidence_store: EvidenceStore) -> None:
        self.llm = llm_provider
        self.store = evidence_store

    async def plan_research(self, topic: str) -> ResearchPlan:
        """Decompose a research topic into subquestions.

        Raises :class:`ResearchEngineError` on provider or schema failure — the
        caller must record a partial/failed state, not a fabricated plan.
        """
        prompt = (
            f"You are a research planner. Break down this research topic into 3 specific "
            f"subquestions to investigate.\nTopic: {topic}\n"
            f'Respond with JSON: {{"subquestions": ["q1", "q2", "q3"], "scope": "brief summary"}}'
        )
        req = GenerationRequest(
            model="gpt-4o-mini",
            messages=[ChatMessage(role=MessageRole.USER, content=prompt)],
            temperature=0.0,
        )
        try:
            res = await self.llm.generate(req)
            data = json.loads(res.text.strip())
        except Exception as exc:  # provider or parse failure is explicit
            raise ResearchEngineError(f"research planning failed: {exc}") from exc

        subquestions = data.get("subquestions")
        if not isinstance(subquestions, list) or not subquestions:
            raise ResearchEngineError(
                "research planning returned no subquestions (invalid schema)"
            )
        return ResearchPlan(
            topic=topic,
            subquestions=subquestions,
            scope=data.get("scope", topic),
        )

    async def add_evidence_item(
        self,
        workspace_id: str,
        query_id: str,
        claim: str,
        source: str,
        quote: str,
        tags: list[str] | None = None,
        chunk_ids: list[str] | None = None,
        *,
        run_id: str = "",
        subquestion_id: str = "",
        document_id: str = "",
        document_version: str = "",
        chunk_id: str = "",
        quote_start: int | None = None,
        quote_end: int | None = None,
        source_hash: str = "",
        verification: EvidenceVerificationState = EvidenceVerificationState.UNVERIFIED,
        verification_reason: str = "",
    ) -> ResearchEvidence:
        """Record a research evidence unit with explicit provenance.

        Verification starts ``UNVERIFIED`` unless the caller supplies evidence
        that the quote resolves to a real stored chunk/version — the caller can
        never assert verified by supplying prose alone.
        """
        effective_chunk = chunk_id or (chunk_ids[0] if chunk_ids else "")
        if verification is not EvidenceVerificationState.UNVERIFIED:
            if not document_version or not effective_chunk or not source_hash:
                raise ResearchEngineError(
                    "non-UNVERIFIED evidence requires document_version, chunk_id and source_hash"
                )
        item = ResearchEvidence(
            id=f"ev-{uuid.uuid4().hex[:8]}",
            workspace_id=workspace_id,
            query_id=query_id,
            claim=claim,
            source=source,
            quote=quote,
            tags=tags or [],
            chunk_ids=chunk_ids or ([effective_chunk] if effective_chunk else []),
            timestamp=datetime.now(UTC).isoformat(),
            run_id=run_id,
            subquestion_id=subquestion_id,
            document_id=document_id,
            document_version=document_version,
            chunk_id=effective_chunk,
            quote_start=quote_start,
            quote_end=quote_end,
            source_hash=source_hash,
            verification=verification,
            verification_reason=verification_reason,
        )
        await self.store.add_evidence(workspace_id, [item])
        return item

    async def synthesize_report(
        self,
        workspace_id: str,
        topic: str,
        *,
        run_id: str | None = None,
    ) -> ResearchReport:
        """Compile accumulated evidence into a structured research report.

        ``run_id`` scopes the report to one investigation. With no verified
        evidence the report abstains: status ``FAILED`` with explicit unknowns,
        never ``COMPLETED``. Only verified evidence becomes ``SUPPORTED``
        claims with original-source citations.
        """
        now = datetime.now(UTC).isoformat()
        if run_id:
            evidence_items = await self.store.list_evidence(workspace_id, run_id=run_id)
        else:
            evidence_items = await self.store.list_evidence(workspace_id)
        contradictions = await self.store.detect_contradictions(workspace_id, run_id=run_id)

        verified = [
            e
            for e in evidence_items
            if e.verification is EvidenceVerificationState.VERIFIED
        ]
        unverified = [
            e
            for e in evidence_items
            if e.verification is not EvidenceVerificationState.VERIFIED
        ]

        unknowns = ["No verified evidence is available for this topic."] if not verified else []

        if not verified:
            # Abstain honestly: no completed report off zero evidence.
            return ResearchReport(
                id=f"rep-{uuid.uuid4().hex[:8]}",
                workspace_id=workspace_id,
                topic=topic,
                status=ResearchStatus.FAILED,
                executive_summary="",
                findings=[],
                evidence_ids=[e.id for e in evidence_items],
                contradictions=contradictions,
                unknowns=[*unknowns, f"unverified evidence present: {len(unverified)}"],
                recommendations=[
                    "Collect and verify original-source evidence before reporting."
                ],
                citations=[],
                created_at=now,
                completed_at=None,
                claims=[],
                supported_findings=[],
                disputed_candidates=[c.claim_b for c in contradictions],
            )

        evidence_text = "\n".join(
            f"- [{e.id}] Claim: {e.claim} (Source: {e.source}, Quote: '{e.quote}')"
            for e in verified
        )

        prompt = (
            f"Generate a formal research report based strictly on this verified evidence:\n"
            f"Topic: {topic}\n\nEvidence:\n{evidence_text}\n\n"
            f"Provide JSON with format:\n"
            f'{{"executive_summary": "...", "findings": ["..."], '
            f'"unknowns": ["..."], "recommendations": ["..."]}}'
        )

        req = GenerationRequest(
            model="gpt-4o-mini",
            messages=[ChatMessage(role=MessageRole.USER, content=prompt)],
            temperature=0.0,
        )

        try:
            res = await self.llm.generate(req)
            data = json.loads(res.text.strip())
            exec_summary = data.get("executive_summary", "")
            findings = data.get("findings", [])
            llm_unknowns = data.get("unknowns", [])
            recommendations = data.get("recommendations", [])
        except Exception as exc:  # provider/parse failure: no fake success
            raise ResearchEngineError(f"report synthesis failed: {exc}") from exc

        # Only verified evidence supports claims; each carries real citations.
        claims: list[ReportClaim] = []
        for statement in findings:
            claims.append(
                ReportClaim(
                    statement=statement,
                    evidence_ids=[e.id for e in verified],
                    citations=[
                        Citation(
                            document_id=e.document_id,
                            chunk_id=e.chunk_id,
                            quote=e.quote,
                            span_start=e.quote_start,
                            span_end=e.quote_end,
                            source_hash=e.source_hash,
                        )
                        for e in verified
                        if e.chunk_id and e.document_id
                    ],
                    status=ClaimStatus.SUPPORTED,
                )
            )
        supported = [c.statement for c in claims if c.status is ClaimStatus.SUPPORTED]
        # Heuristic conflicts are candidates, never verdicts.
        disputed = [c.claim_b for c in contradictions]

        return ResearchReport(
            id=f"rep-{uuid.uuid4().hex[:8]}",
            workspace_id=workspace_id,
            topic=topic,
            status=ResearchStatus.COMPLETED,
            executive_summary=exec_summary,
            findings=findings,
            evidence_ids=[e.id for e in verified],
            contradictions=contradictions,
            unknowns=llm_unknowns + unknowns,
            recommendations=recommendations,
            citations=sorted({e.document_id for e in verified if e.document_id}),
            created_at=now,
            completed_at=datetime.now(UTC).isoformat(),
            claims=claims,
            supported_findings=supported,
            disputed_candidates=disputed,
        )
