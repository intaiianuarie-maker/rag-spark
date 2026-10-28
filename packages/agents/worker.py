"""Run worker — consumes queued agent runs with V1 ingestion discipline (T-V3-013).

Reuses the V1 claim/lease/token fence, ack only with current token, expired
recovery to ready, at-least-once via checkpoint idempotence, visible park codes.
Picks up a queued run, resumes from its checkpoint (T-012 bridge), executes to
terminal, checkpoints along the way. A worker dying mid-run leaves the run
re-claimable with its checkpoint intact (kill-mid-run test with lease expiry).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .interfaces import AgentRunStatus, RunStatus
from .queue import AgentRunQueue
from .runtime import AgentRuntime

# Park codes — visible, not silent
PARK_CODE_MISSING_CONTEXT = "MISSING_CONTEXT"
PARK_CODE_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
PARK_CODE_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
PARK_CODE_WORKER_ERROR = "WORKER_ERROR"


logger = logging.getLogger("knowledge_os.agents.worker")


class AgentWorker:
    """Consumes `AgentRunQueue` with durable claim/ack, checkpoint resume."""

    def __init__(
        self,
        queue: AgentRunQueue,
        runtime: AgentRuntime,
        context_store: dict[str, tuple[Any, str]] | None = None,
    ) -> None:
        # context_store: run_id -> (RunContext, goal)
        self.queue = queue
        self.runtime = runtime
        self.context_store: dict[str, tuple[Any, str]] = context_store if context_store is not None else {}  # noqa: E501

    async def run_once(self, lease_seconds: float | None = None) -> str | None:
        """Claim one run, resume from checkpoint, execute to terminal, ack/park.

        Returns the run_id that was claimed, or None if nothing to claim.
        Never acks a run that is still WAITING_APPROVAL — it stays claimed
        until lease expires and is recovered, with its checkpoint intact.
        """
        claim = await self.queue.claim(lease_seconds)
        if claim is None:
            return None
        run_id = str(claim)
        # Resolve context + goal for this run_id
        ctx_goal = self.context_store.get(run_id)
        if ctx_goal is None:
            # No context — park visibly, don't re-queue forever
            try:
                await self.queue.park(claim, PARK_CODE_MISSING_CONTEXT)
            except Exception as exc:
                logger.warning("agent_queue_park_failed", extra={"error": str(exc)})
            return run_id
        context, goal = ctx_goal
        try:
            run = await self.runtime.run_enforced(context=context, goal=goal)
            # Decide ack vs park vs keep claimed
            # run.status is AgentRunStatus, run.error may contain code
            # Also need RunState for WAITING_APPROVAL — check via runtime's persisted state
            from .runtime import _PERSISTED_STATES

            state = _PERSISTED_STATES.get(run_id)
            # If still WAITING_APPROVAL, keep claimed (don't ack) — will be recovered after lease
            if state is not None and state.status == RunStatus.WAITING_APPROVAL:
                # Keep claimed, don't ack — checkpoint already written, will be re-claimed after lease  # noqa: E501
                return run_id
            # If terminal, ack (or park if deterministically failed)
            if run.status in (
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.MAX_STEPS_EXCEEDED,
                AgentRunStatus.BUDGET_EXCEEDED,
            ):
                # Park deterministically failed (e.g. TOOL_NOT_ALLOWED with no retry) vs ack
                # For now, treat FAILED with TOOL_NOT_ALLOWED as park, otherwise ack
                if run.status == AgentRunStatus.FAILED and run.error and "tool_not_allowed" in run.error.lower():  # noqa: E501
                    try:
                        await self.queue.park(claim, PARK_CODE_NOT_ALLOWED)
                    except Exception:
                        await self.queue.ack(claim)
                elif run.status == AgentRunStatus.BUDGET_EXCEEDED or (run.error and "budget_exceeded" in run.error.lower()):  # noqa: E501
                    try:
                        await self.queue.park(claim, PARK_CODE_BUDGET_EXCEEDED)
                    except Exception:
                        await self.queue.ack(claim)
                else:
                    await self.queue.ack(claim)
                return run_id
            # Still running? Should not happen, as run_enforced is to terminal or waiting
            await self.queue.ack(claim)
            return run_id
        except asyncio.CancelledError:
            # Worker killed mid-run — don't ack, let lease expire, checkpoint intact
            raise
        except Exception as exc:
            # Worker error — park visibly
            try:
                await self.queue.park(claim, f"{PARK_CODE_WORKER_ERROR}:{type(exc).__name__}")
            except Exception:
                try:
                    await self.queue.ack(claim)
                except Exception:
                    pass
            return run_id

    async def run_forever(self, lease_seconds: float | None = None, poll_interval: float = 0.1) -> None:  # noqa: E501
        """Loop forever, recovering expired claims and claiming ready runs."""
        while True:
            # Recover any expired claims first (at-least-once)
            try:
                await self.queue.recover_expired()
            except Exception as exc:
                logger.warning("agent_queue_recover_failed", extra={"error": str(exc)})
            claimed = await self.run_once(lease_seconds)
            if claimed is None:
                await asyncio.sleep(poll_interval)

    # For tests: helper to get queue depth
    async def depth(self) -> int:
        return await self.queue.depth()

    async def parked_depth(self) -> int:
        return await self.queue.parked_depth()
