"""Research Engine implementation (Section 28).

Runs multi-step investigations: decomposes topic into subquestions,
persists verified evidence, detects contradictions, and synthesizes structured reports.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from models.interfaces import ChatMessage, GenerationRequest, LLMProvider, MessageRole

from .evidence_store import EvidenceStore
from .interfaces import (
    ResearchEvidence,
    ResearchPlan,
    ResearchReport,
    ResearchStatus,
)


class ResearchEngine:
    """Multi-step research and synthesis engine."""

    def __init__(self, llm_provider: LLMProvider, evidence_store: EvidenceStore) -> None:
        self.llm = llm_provider
        self.store = evidence_store

    async def plan_research(self, topic: str) -> ResearchPlan:
        """Decompose a research topic into subquestions."""
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
            return ResearchPlan(
                topic=topic,
                subquestions=data.get("subquestions", [topic]),
                scope=data.get("scope", topic),
            )
        except Exception:
            # Fallback plan
            return ResearchPlan(
                topic=topic,
                subquestions=[
                    f"What are key aspects of {topic}?",
                    f"What are risks or challenges of {topic}?",
                ],
                scope=topic,
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
    ) -> ResearchEvidence:
        """Record a verified research evidence unit."""
        item = ResearchEvidence(
            id=f"ev-{uuid.uuid4().hex[:8]}",
            workspace_id=workspace_id,
            query_id=query_id,
            claim=claim,
            source=source,
            quote=quote,
            tags=tags or [],
            chunk_ids=chunk_ids or [],
            timestamp=datetime.now(UTC).isoformat(),
        )
        await self.store.add_evidence(workspace_id, [item])
        return item

    async def synthesize_report(
        self,
        workspace_id: str,
        topic: str,
    ) -> ResearchReport:
        """Compile accumulated evidence into a structured research report."""
        now = datetime.now(UTC).isoformat()
        evidence_items = await self.store.list_evidence(workspace_id)
        contradictions = await self.store.detect_contradictions(workspace_id)

        evidence_text = "\n".join(
            f"- [{e.id}] Claim: {e.claim} (Source: {e.source}, Quote: '{e.quote}')"
            for e in evidence_items
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
            exec_summary = data.get("executive_summary", f"Research report on {topic}")
            findings = data.get("findings", [e.claim for e in evidence_items])
            unknowns = data.get("unknowns", [])
            recommendations = data.get("recommendations", [])
        except Exception:
            exec_summary = f"Summary on {topic} based on {len(evidence_items)} evidence items."
            findings = [e.claim for e in evidence_items]
            unknowns = ["Additional source verification required."]
            recommendations = ["Review collected evidence."]

        citations = sorted(list({e.source for e in evidence_items}))

        return ResearchReport(
            id=f"rep-{uuid.uuid4().hex[:8]}",
            workspace_id=workspace_id,
            topic=topic,
            status=ResearchStatus.COMPLETED,
            executive_summary=exec_summary,
            findings=findings,
            evidence_ids=[e.id for e in evidence_items],
            contradictions=contradictions,
            unknowns=unknowns,
            recommendations=recommendations,
            citations=citations,
            created_at=now,
            completed_at=datetime.now(UTC).isoformat(),
        )
