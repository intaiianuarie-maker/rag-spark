"""V3 Real Research Pipeline (T-R16).

Pipeline stages:
1. Planning: topic -> policy-derived fan-out plan with sub-questions and tool assignments.
   Derives constraints via `derive_effective_run_config` (widening strictly refused).
2. Fan-out execution: sub-questions executed via `create_agent_runtime` with per-run
   budgets enforced (spend tracked, caps trip loudly — R14 machinery).
3. Source evidence collection: real retrieved chunks with provenance, ACL-scoped.
4. Verification pass: claim/citation check before synthesis via ExtractiveGrounder
   seam (uncited/unsupported claims rejected into unknowns; deletion revalidation).
5. Gaps + Contradictions + Synthesis: honest gaps stated, contradictions surfaced with
   capped confidence, grounded sentences only in final report.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from answer.verification import ExtractiveGrounder
from models.interfaces import LLMProvider

from .contradictions import ContradictionDetector
from .evidence import Citation, EvidenceRecord, EvidenceStore, InMemoryEvidenceStore
from .factories import create_agent_runtime
from .gap import (
    REASON_NO_EVIDENCE,
    REASON_NOT_GROUNDED,
    GapAnalyzer,
    Unknown,
)
from .interfaces import (
    AgentErrorCode,
    AgentRun,
    AgentRunStatus,
    ModelPolicy,
    RunBudget,
    RunContext,
)
from .planner import (
    InMemoryPlanStore,
    PlanStore,
    StormPlan,
    StormPlanner,
)
from .policy import (
    EffectiveRunConfig,
    PolicyViolationError,
    derive_effective_run_config,
    get_workspace_policy,
)
from .synthesis import GroundedSynthesizer
from .tools import Tool
from .verifier import (
    CAUSE_GROUNDED,
    ChunkSource,
    EvidenceVerifier,
    InMemoryChunkSource,
)

MAX_RESEARCH_QUESTIONS = 9


def _extract_sentences(text: str) -> list[str]:
    """Split text into sentences, stripping surrounding whitespace."""
    if not text:
        return []
    # Split on periods/exclamations/questions followed by space or newline
    raw_sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    sentences = [s.strip() for s in raw_sentences if s.strip()]
    return sentences


def _extract_chunk_ids(sentence: str) -> list[str]:
    """Extract chunk ids from explicit citations like [chunk-1] or [chk-1]."""
    matches = re.findall(r"\[([a-zA-Z0-9_\-\.:]+)\]", sentence)
    return [m for m in matches if not m.isdigit()]


def _chunk_source_hash(content: str) -> str:
    """Compute a deterministic source hash for canonical citation provenance.

    T-A03: every citation carries a ``source_hash`` so re-verification after
    a delete or version-replace can detect that the content backing the claim
    changed. The hash is computed from the canonical chunk content at retrieval
    time and stored on the citation.
    """
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


class ResearchPipeline:
    """End-to-end research pipeline executing planning -> fan-out -> verify -> synthesis."""

    def __init__(
        self,
        llm: LLMProvider,
        *,
        tools: list[Tool] | None = None,
        orchestrator: Any | None = None,
        chunk_reader: Any | None = None,
        chunk_source: ChunkSource | None = None,
        grounder: ExtractiveGrounder | None = None,
        plan_store: PlanStore | None = None,
        evidence_store: EvidenceStore | None = None,
        call_site: str = "agents.research",
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.orchestrator = orchestrator
        self.chunk_reader = chunk_reader
        self.chunk_source = chunk_source or InMemoryChunkSource()
        self.grounder = grounder or ExtractiveGrounder()
        self.plan_store = plan_store or InMemoryPlanStore()
        self.evidence_store = evidence_store or InMemoryEvidenceStore()
        self.call_site = call_site

    async def run(
        self,
        topic: str,
        workspace_id: str,
        principal_id: str,
        *,
        max_questions: int | None = None,
        requested_model: str | None = None,
        requested_tools: list[str] | None = None,
        requested_steps: int | None = None,
        requested_tokens: int | None = None,
        requested_tool_calls: int | None = None,
        requested_cost_usd: float | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute the full research pipeline and return the honesty package."""
        clean_topic = topic.strip()
        if not clean_topic:
            raise ValueError("topic must be non-empty")

        research_id = str(uuid.uuid4())
        active_trace_id = trace_id or research_id

        # ------------------------------------------------------------------
        # Stage 1: Server-policy derivation and Planning
        # ------------------------------------------------------------------
        policy = get_workspace_policy(workspace_id)

        # Check question count limits against policy and system bounds
        if max_questions is not None:
            if max_questions > policy.max_steps:
                raise PolicyViolationError("max_questions", max_questions, policy.max_steps)
            if max_questions > MAX_RESEARCH_QUESTIONS:
                raise PolicyViolationError("max_questions", max_questions, MAX_RESEARCH_QUESTIONS)

        effective_cfg: EffectiveRunConfig = derive_effective_run_config(
            policy,
            goal=clean_topic,
            requested_model=requested_model,
            requested_tools=requested_tools,
            requested_steps=requested_steps,
            requested_tokens=requested_tokens,
            requested_tool_calls=requested_tool_calls,
            requested_cost_usd=requested_cost_usd,
        )

        target_q_count = min(max_questions or 3, effective_cfg.max_steps, MAX_RESEARCH_QUESTIONS)
        target_q_count = max(1, target_q_count)

        # T-GEMMA-001: Planner uses canonical Gemma route — free-first, paid
        # fallback only after workspace budget check (fail-closed).
        from .policy import resolve_gemma_route

        planner_model = resolve_gemma_route(workspace_id)

        planner = StormPlanner(
            llm=self.llm,
            plan_store=self.plan_store,
            model_name=planner_model,
            workspace_id=workspace_id,
        )
        plan: StormPlan = await planner.create_plan(
            clean_topic,
            run_id=f"plan-{research_id}",
            trace_id=active_trace_id,
        )
        sub_questions = plan.questions[:target_q_count] if plan.questions else [clean_topic]

        # ------------------------------------------------------------------
        # Stage 2: Fan-out Execution via create_agent_runtime with budgets
        # ------------------------------------------------------------------
        sub_budget = RunBudget(
            max_steps=effective_cfg.max_steps,
            max_tokens=effective_cfg.max_tokens,
            max_tool_calls=effective_cfg.max_tool_calls,
            max_cost_usd=effective_cfg.max_cost_usd,
            max_tool_cost_usd=effective_cfg.max_tool_cost_usd,
            max_wall_clock_s=effective_cfg.max_wall_clock_s,
        )

        worker_runs: list[AgentRun] = []
        budget_exhausted_questions: list[str] = []

        for idx, question in enumerate(sub_questions):
            sub_run_id = f"{research_id}-q{idx + 1}"
            sub_ctx = RunContext(
                run_id=sub_run_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                authz_policy_revision="rev1",
                model_policy=ModelPolicy(
                    allowed_models=effective_cfg.allowed_models,
                    default_model=effective_cfg.model,
                ),
                budget=sub_budget,
                deadline=datetime.now(UTC)
                + timedelta(seconds=min(60.0, effective_cfg.max_wall_clock_s)),
                allowed_tools=effective_cfg.allowed_tools,
            )

            runtime = create_agent_runtime(
                llm=self.llm,
                tools=self.tools,
                orchestrator=self.orchestrator,
                chunk_reader=self.chunk_reader,
                require_llm=True,
                call_site=self.call_site,
            )

            run_record: AgentRun = await runtime.run_enforced(
                context=sub_ctx,
                goal=question,
            )
            worker_runs.append(run_record)

            from .runtime import _PERSISTED_STATES

            durable_state = _PERSISTED_STATES.get(run_record.id)
            is_budget_trip = (
                (
                    durable_state is not None
                    and durable_state.error_code == AgentErrorCode.BUDGET_EXCEEDED
                )
                or (run_record.error and "budget" in run_record.error.lower())
                or run_record.status == AgentRunStatus.MAX_STEPS_EXCEEDED
            )
            if is_budget_trip:
                budget_exhausted_questions.append(question)

        # ------------------------------------------------------------------
        # Stage 3: Source Evidence Collection (Real Chunks, ACL-Scoped)
        # ------------------------------------------------------------------
        candidate_records: list[tuple[str, EvidenceRecord]] = []
        uncited_claims: list[tuple[str, str]] = []  # (question, claim)
        searched_queries: list[str] = list(sub_questions)

        for q_idx, run_rec in enumerate(worker_runs):
            q_text = sub_questions[q_idx]
            q_chunk_ids: list[str] = []
            q_chunk_map: dict[str, dict[str, Any]] = {}

            # Extract chunks from search and read steps
            for step in run_rec.steps:
                tool_output = step.tool_output or {}
                if step.tool_name == "knowledge_search" and isinstance(tool_output, dict):
                    results = tool_output.get("results") or []
                    for item in results:
                        cid = item.get("chunk_id")
                        content = item.get("content", "")
                        doc_id = item.get("document_id", "")
                        ws = item.get("workspace_id", workspace_id)
                        if cid and content:
                            is_tomb = (
                                hasattr(self.chunk_source, "is_tombstoned")
                                and self.chunk_source.is_tombstoned(cid)
                            )
                            if not is_tomb and hasattr(self.chunk_source, "put"):
                                self.chunk_source.put(cid, content)
                            q_chunk_ids.append(cid)
                            q_chunk_map[cid] = {
                                "chunk_id": cid,
                                "document_id": doc_id,
                                "workspace_id": ws,
                                "content": content,
                            }
                elif step.tool_name == "knowledge_read" and isinstance(tool_output, dict):
                    cid = tool_output.get("chunk_id")
                    content = tool_output.get("content", "")
                    doc_id = tool_output.get("document_id", "")
                    ws = tool_output.get("workspace_id", workspace_id)
                    if cid and content:
                        is_tomb = (
                            hasattr(self.chunk_source, "is_tombstoned")
                            and self.chunk_source.is_tombstoned(cid)
                        )
                        if not is_tomb and hasattr(self.chunk_source, "put"):
                            self.chunk_source.put(cid, content)
                        q_chunk_ids.append(cid)
                        q_chunk_map[cid] = {
                            "chunk_id": cid,
                            "document_id": doc_id,
                            "workspace_id": ws,
                            "content": content,
                        }

            output_text = run_rec.final_output or ""
            sentences = _extract_sentences(output_text)

            for sentence in sentences:
                cited_in_sentence = _extract_chunk_ids(sentence)
                target_chunks = cited_in_sentence or q_chunk_ids

                if not target_chunks:
                    # Metric 2: Uncited claim without evidence chunks
                    uncited_claims.append((q_text, sentence))
                    continue

                citations = [
                    Citation(
                        document_id=q_chunk_map.get(cid, {}).get("document_id", ""),
                        chunk_id=cid,
                        quote=q_chunk_map.get(cid, {}).get("content", "")[:100],
                        source_hash=_chunk_source_hash(
                            q_chunk_map.get(cid, {}).get("content", "")
                        ),
                    )
                    for cid in target_chunks
                ]

                # Strip explicit bracket citations for clean statement
                clean_statement = re.sub(r"\[[a-zA-Z0-9_\-\.:]+\]", "", sentence).strip()
                if not clean_statement:
                    continue

                rec = EvidenceRecord(
                    run_id=run_rec.id,
                    trace_id=active_trace_id,
                    workspace_id=workspace_id,
                    statement=clean_statement,
                    chunk_ids=target_chunks,
                    citations=citations,
                    verdict="SUPPORTED",
                    source_hash=citations[0].source_hash if citations else "",
                )
                candidate_records.append((q_text, rec))

        # ------------------------------------------------------------------
        # Stage 4: Verification Pass (Claim/Citation check before synthesis)
        # ------------------------------------------------------------------
        verifier = EvidenceVerifier(self.chunk_source, grounder=self.grounder)
        verified_claims: list[EvidenceRecord] = []
        verified_by_question: dict[str, list[EvidenceRecord]] = {q: [] for q in sub_questions}
        searched_by_question: dict[str, list[str]] = {q: [q] for q in sub_questions}
        unverified_unknowns: list[Unknown] = []

        for q_text, rec in candidate_records:
            reverify_res = await verifier.reverify_record(rec)
            if reverify_res.agree and reverify_res.new_verdict == "SUPPORTED":
                verified_claims.append(rec)
                verified_by_question[q_text].append(rec)
                if self.evidence_store is not None:
                    if hasattr(self.evidence_store, "write"):
                        await self.evidence_store.write(rec)
                    elif hasattr(self.evidence_store, "save"):
                        await self.evidence_store.save(rec)
            else:
                # Metric 2: Verifier rejects uncited or ungrounded claims
                reason = reverify_res.cause
                if reason == CAUSE_GROUNDED:
                    reason = REASON_NOT_GROUNDED
                unverified_unknowns.append(
                    Unknown(
                        question=rec.statement,
                        searched=reverify_res.chunk_ids_checked or rec.chunk_ids,
                        reason=reason,
                        tried_chunk_ids=reverify_res.chunk_ids_checked or rec.chunk_ids,
                    )
                )

        # Uncited claims are rejected straight into unknowns
        for q_text, uncited_stmt in uncited_claims:
            clean_stmt = re.sub(r"\[[a-zA-Z0-9_\-\.:]+\]", "", uncited_stmt).strip()
            unverified_unknowns.append(
                Unknown(
                    question=clean_stmt or q_text,
                    searched=[q_text],
                    reason=REASON_NO_EVIDENCE,
                    tried_chunk_ids=[],
                )
            )

        # ------------------------------------------------------------------
        # Stage 5: Gaps + Contradictions + Synthesis
        # ------------------------------------------------------------------
        gap_analyzer = GapAnalyzer(
            self.chunk_source,
            grounder=self.grounder,
        )
        gap_report = await gap_analyzer.analyze(
            sub_questions,
            evidence_by_question=verified_by_question,
            searched_by_question=searched_by_question,
        )
        gap_unknowns: list[Unknown] = list(gap_report.unknowns)

        for q_text in budget_exhausted_questions:
            # Budget exhausted overrides reason for this question
            gap_unknowns = [u for u in gap_unknowns if u.question != q_text]
            gap_unknowns.append(
                Unknown(
                    question=q_text,
                    searched=searched_by_question.get(q_text, [q_text]),
                    reason="BUDGET_EXHAUSTED",
                    tried_chunk_ids=[],
                )
            )

        contradiction_detector = ContradictionDetector(
            self.chunk_source,
            grounder=self.grounder,
        )
        contradiction_rep = await contradiction_detector.detect(
            sub_questions,
            verified_by_question,
        )

        synthesizer = GroundedSynthesizer(
            self.chunk_source,
            grounder=self.grounder,
        )

        all_unknowns = gap_unknowns + unverified_unknowns

        synthesis_result = await synthesizer.synthesize(
            questions=sub_questions,
            verified_claims=verified_claims,
            candidates=contradiction_rep.candidates,
            gap_unknowns=all_unknowns,
            contradictions=contradiction_rep.contradictions,
            contradiction_outline=contradiction_rep.outline_sections,
            searched=searched_queries,
        )

        # Build final honesty package matching ResearchReportResponse
        grounded_data: list[dict[str, Any]] = [
            {
                "sentence": g.sentence,
                "chunk_ids": g.chunk_ids,
                "evidence_ids": g.evidence_ids,
                "confidence": g.confidence,
                "citations": g.citations,
            }
            for g in synthesis_result.grounded_sentences
        ]

        unknowns_data: list[dict[str, Any]] = [
            {
                "question": u.question,
                "searched": u.searched,
                "reason": u.reason,
                "tried_chunk_ids": u.tried_chunk_ids,
            }
            for u in synthesis_result.unknowns
        ]

        contradictions_data: list[dict[str, Any]] = [
            {
                "question": c.question,
                "claim_a": c.claim_a,
                "claim_b": c.claim_b,
                "evidence_a_id": c.evidence_a_id,
                "evidence_b_id": c.evidence_b_id,
                "chunk_ids_a": c.chunk_ids_a,
                "chunk_ids_b": c.chunk_ids_b,
                "reason": c.reason,
            }
            for c in synthesis_result.contradictions
        ]

        # Collect unique citations across all grounded sentences
        citations_list: list[str] = list(
            dict.fromkeys(
                cid for g in synthesis_result.grounded_sentences for cid in g.citations
            )
        )

        return {
            "research_id": research_id,
            "topic": clean_topic,
            "grounded_sentences": grounded_data,
            "unknowns": unknowns_data,
            "contradictions": contradictions_data,
            "contradiction_outline": synthesis_result.contradiction_outline,
            "citations": citations_list,
            "abstained": synthesis_result.abstained,
            "abstention_reason": synthesis_result.abstention_reason,
            "searched": synthesis_result.searched,
            "limitations": synthesis_result.limitations,
            "error_code": None,
            "missing": None,
        }

    async def __call__(
        self,
        topic: str,
        workspace_id: str,
        principal_id: str,
        *,
        max_questions: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.run(
            topic,
            workspace_id,
            principal_id,
            max_questions=max_questions,
            **kwargs,
        )


def create_research_pipeline_runner(
    llm: LLMProvider,
    *,
    tools: list[Tool] | None = None,
    orchestrator: Any | None = None,
    chunk_reader: Any | None = None,
    chunk_source: ChunkSource | None = None,
    grounder: ExtractiveGrounder | None = None,
    plan_store: PlanStore | None = None,
    evidence_store: EvidenceStore | None = None,
    call_site: str = "agents.research",
) -> Callable[..., Any]:
    """Create a research pipeline runner callable for apps/api or jobs."""
    pipeline = ResearchPipeline(
        llm=llm,
        tools=tools,
        orchestrator=orchestrator,
        chunk_reader=chunk_reader,
        chunk_source=chunk_source,
        grounder=grounder,
        plan_store=plan_store,
        evidence_store=evidence_store,
        call_site=call_site,
    )

    async def _runner(
        topic: str,
        workspace_id: str,
        principal_id: str,
        max_questions: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await pipeline.run(
            topic,
            workspace_id,
            principal_id,
            max_questions=max_questions,
            **kwargs,
        )

    return _runner
