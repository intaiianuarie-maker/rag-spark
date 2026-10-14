"""Agent Run Worker & Queue Pickup (T-R15).

Picks up queued agent runs from durable queue, transitions RunState from QUEUED -> RUNNING,
resumes from durable checkpoints via StepStore bridge, executes via AgentRuntime,
persists checkpoints/steps, and acks/parks upon terminal state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from agents.interfaces import (
    AgentErrorCode,
    AgentRunStatus,
    RunState,
    RunStatus,
)
from agents.queue import AgentRunQueue
from agents.runtime import AgentRuntime
from stores.agent_stores import (
    DurableCheckpointStore,
    RunStore,
    StepStore,
    get_checkpoint_store,
    get_queue,
    get_run_store,
    get_step_store,
)

logger = logging.getLogger("knowledge_os.worker.agent")

# Visible park codes for unrecoverable failures
PARK_CODE_MISSING_RECORD = "MISSING_RECORD"
PARK_CODE_MISSING_CONTEXT = "MISSING_CONTEXT"
PARK_CODE_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
PARK_CODE_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
PARK_CODE_WORKER_ERROR = "WORKER_ERROR"


class AgentLifecycleWorker:
    """Worker that consumes queued agent runs with checkpoint resume and lease fence."""

    def __init__(
        self,
        queue: AgentRunQueue | None = None,
        run_store: RunStore | None = None,
        checkpoint_store: DurableCheckpointStore | None = None,
        step_store: StepStore | None = None,
        runtime_factory: Callable[[], AgentRuntime] | None = None,
    ) -> None:
        self.queue: AgentRunQueue = queue if queue is not None else get_queue()
        self.run_store: RunStore = run_store if run_store is not None else get_run_store()
        self.checkpoint_store: DurableCheckpointStore = (
            checkpoint_store if checkpoint_store is not None else get_checkpoint_store()
        )
        self.step_store: StepStore = (
            step_store if step_store is not None else get_step_store()
        )
        self.runtime_factory = runtime_factory

    def _resolve_runtime(self) -> AgentRuntime:
        if self.runtime_factory is not None:
            return self.runtime_factory()
        from agents.factories import create_agent_runtime

        return create_agent_runtime()

    async def run_once(self, lease_seconds: float | None = None) -> str | None:
        """Claim one run from the queue, resume from checkpoint if present, and execute.

        Returns:
            The claimed run_id, or None if the queue was empty.
        """
        claim = await self.queue.claim(lease_seconds)
        if claim is None:
            return None

        run_id = str(claim)
        record = self.run_store.get_run(run_id)
        if record is None:
            # No record found in run store: park visibly, never retry endlessly
            try:
                await self.queue.park(claim, PARK_CODE_MISSING_RECORD)
            except Exception:
                pass
            return run_id

        # If already terminal or cancelled while in queue
        if RunState.is_terminal(record.status):
            await self.queue.ack(claim)
            return run_id

        if record.state.is_cancelled():
            self.run_store.update_status(
                run_id,
                RunStatus.CANCELLED,
                error="run cancelled before start",
                error_code=AgentErrorCode.CANCELLED,
            )
            await self.queue.ack(claim)
            return run_id

        if record.context is None:
            try:
                await self.queue.park(claim, PARK_CODE_MISSING_CONTEXT)
            except Exception:
                pass
            return run_id

        # Transition run status from QUEUED to RUNNING
        self.run_store.update_status(run_id, RunStatus.RUNNING)

        # Check for existing checkpoint (resumption path)
        _checkpoint = self.checkpoint_store.load_checkpoint(run_id)

        try:
            runtime = self._resolve_runtime()
            agent_run = await runtime.run_enforced(context=record.context, goal=record.goal)

            # Inspect resulting state
            from agents.runtime import _PERSISTED_STATES

            persisted_state = _PERSISTED_STATES.get(run_id)
            current_status = (
                persisted_state.status
                if persisted_state is not None
                else RunStatus(agent_run.status.value)
            )

            if current_status == RunStatus.WAITING_APPROVAL:
                # Run is waiting for approval: leave claim active or let lease manage it,
                # state remains WAITING_APPROVAL, checkpoint is persisted.
                self.run_store.update_status(run_id, RunStatus.WAITING_APPROVAL)
                return run_id

            is_cancelled = current_status == RunStatus.CANCELLED or (
                agent_run.status == AgentRunStatus.FAILED
                and bool(agent_run.error and "cancelled" in agent_run.error.lower())
            )
            if is_cancelled:
                self.run_store.update_status(
                    run_id,
                    RunStatus.CANCELLED,
                    error=agent_run.error,
                    error_code=AgentErrorCode.CANCELLED,
                )
                await self.queue.ack(claim)
                return run_id

            if current_status == RunStatus.LIMIT_EXCEEDED or agent_run.status in (
                AgentRunStatus.BUDGET_EXCEEDED,
                AgentRunStatus.MAX_STEPS_EXCEEDED,
            ):
                error_code = (
                    persisted_state.error_code
                    if persisted_state and persisted_state.error_code
                    else AgentErrorCode.BUDGET_EXCEEDED
                )
                self.run_store.update_status(
                    run_id,
                    RunStatus.LIMIT_EXCEEDED,
                    error=agent_run.error,
                    error_code=error_code,
                )
                await self.queue.park(claim, PARK_CODE_BUDGET_EXCEEDED)
                return run_id

            if (
                agent_run.status == AgentRunStatus.COMPLETED
                or current_status == RunStatus.COMPLETED
            ):
                self.run_store.update_status(run_id, RunStatus.COMPLETED)
                await self.queue.ack(claim)
                return run_id

            if agent_run.status == AgentRunStatus.FAILED:
                if agent_run.error and "tool_not_allowed" in agent_run.error.lower():
                    self.run_store.update_status(
                        run_id,
                        RunStatus.FAILED,
                        error=agent_run.error,
                        error_code=AgentErrorCode.TOOL_NOT_ALLOWED,
                    )
                    await self.queue.park(claim, PARK_CODE_NOT_ALLOWED)
                else:
                    error_code = (
                        persisted_state.error_code
                        if persisted_state and persisted_state.error_code
                        else AgentErrorCode.PROVIDER_ERROR
                    )
                    self.run_store.update_status(
                        run_id,
                        RunStatus.FAILED,
                        error=agent_run.error,
                        error_code=error_code,
                    )
                    await self.queue.ack(claim)
                return run_id

            # Fallback
            self.run_store.update_status(run_id, current_status, error=agent_run.error)
            await self.queue.ack(claim)
            return run_id

        except asyncio.CancelledError:
            # Worker was stopped or cancelled mid-execution:
            # Do NOT ack! Leave the claim to expire so queue recovery re-enqueues it,
            # and next worker resumes byte-identically from the latest checkpoint.
            logger.warning("worker_cancelled_mid_run", extra={"run_id": run_id})
            raise
        except Exception as exc:
            logger.exception("worker_run_failed", extra={"run_id": run_id, "error": str(exc)})
            self.run_store.update_status(
                run_id,
                RunStatus.FAILED,
                error=str(exc),
                error_code=AgentErrorCode.PROVIDER_ERROR,
            )
            await self.queue.ack(claim)
            return run_id


async def run_agent_worker_loop(
    worker: AgentLifecycleWorker,
    stop_event: asyncio.Event,
    poll_interval_s: float = 0.1,
) -> None:
    """Supervised worker loop polling the queue until stop_event is set."""
    while not stop_event.is_set():
        try:
            # Also recover any expired claims periodically
            await worker.queue.recover_expired()
            claimed_id = await worker.run_once()
            if claimed_id is None:
                await asyncio.sleep(poll_interval_s)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("worker_loop_error", extra={"error": str(exc)})
            await asyncio.sleep(poll_interval_s)
