"""Agent run queue — durable claim/ack with lease, visible park codes (T-V3-013).

Reuses the V1 ingestion discipline (claim/lease with token fence, ack only
with current token, expired claims recover to ready, at-least-once via
checkpoint idempotence). Keys are `agent:<env>:...` so tests never collide
with the live ingestion queue (`ingest:`) or with each other (via
`AGENT_QUEUE_PREFIX`).

Park codes are visible: `PARKED` is a terminal zset for deterministically
failed runs that should not be retried (e.g. `TOOL_NOT_ALLOWED` with no
retryable policy). The worker parks instead of re-queuing, so `orphaned
(claimed-never-finished) runs after lease: 0` holds.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from abc import ABC, abstractmethod


class Claim(str):
    """Claimed run_id plus lease fence token — same shape as `ingestion Claim`."""

    __slots__ = ("token",)

    token: str

    def __new__(cls, run_id: str, token: str) -> Claim:
        obj = super().__new__(cls, run_id)
        obj.token = token
        return obj


def _prefix() -> str:
    # Tests set AGENT_QUEUE_PREFIX to isolate; otherwise use app_env
    override = os.environ.get("AGENT_QUEUE_PREFIX")
    if override:
        return override
    try:
        from core.config import get_settings

        return f"{get_settings().app_env}:"
    except Exception:
        import os as _os

        return f"{_os.environ.get('APP_ENV', 'prod')}-fallback:"


def queue_key() -> str:
    return f"agent:{_prefix()}queue"


def claims_key() -> str:
    return f"agent:{_prefix()}claims"


def claim_tokens_key() -> str:
    return f"agent:{_prefix()}claim_tokens"


def parked_key() -> str:
    return f"agent:{_prefix()}parked"


def _now() -> float:
    return time.time()


def lease_seconds() -> float:
    raw = os.environ.get("AGENT_QUEUE_LEASE_SECONDS")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 120.0


class AgentRunQueue(ABC):
    """Queue for agent runs — claim/ack with lease, park, recover."""

    @abstractmethod
    async def enqueue(self, run_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def claim(self, lease_seconds: float | None = None) -> Claim | None:
        raise NotImplementedError

    @abstractmethod
    async def ack(self, claim: Claim) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def renew(self, claim: Claim, lease_seconds: float | None = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def recover_expired(self) -> int:
        raise NotImplementedError

    @abstractmethod
    async def depth(self) -> int:
        raise NotImplementedError

    @abstractmethod
    async def parked_depth(self) -> int:
        raise NotImplementedError

    @abstractmethod
    async def park(self, claim: Claim, code: str) -> bool:
        """Move a claimed run to PARKED with a visible park code (terminal, no retry)."""
        raise NotImplementedError


class InMemoryAgentRunQueue(AgentRunQueue):
    """In-memory fake that mirrors the Redis Lua claim/ack/recover exactly, for unit tests.

    `ready` is a list (RPOP), `claims` is dict run_id -> deadline, `tokens` is dict run_id -> token,
    `parked` is dict run_id -> (code, ts). All ops are atomic under `asyncio.Lock`.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._ready: list[str] = []
        self._claims: dict[str, float] = {}
        self._tokens: dict[str, str] = {}
        self._parked: dict[str, tuple[str, float]] = {}

    async def enqueue(self, run_id: str) -> None:
        async with self._lock:
            # Don't enqueue if already queued or claimed or parked
            if run_id in self._ready or run_id in self._claims or run_id in self._parked:
                return
            self._ready.append(run_id)

    async def claim(self, lease_seconds: float | None = None) -> Claim | None:
        ls = lease_seconds if lease_seconds is not None else globals()["lease_seconds"]()
        async with self._lock:
            if not self._ready:
                return None
            run_id = self._ready.pop(0)
            token = uuid.uuid4().hex
            deadline = _now() + ls
            self._claims[run_id] = deadline
            self._tokens[run_id] = token
            return Claim(run_id, token)

    async def ack(self, claim: Claim) -> bool:
        async with self._lock:
            run_id = str(claim)
            token = getattr(claim, "token", None)
            if self._tokens.get(run_id) != token:
                return False
            self._claims.pop(run_id, None)
            self._tokens.pop(run_id, None)
            return True

    async def renew(self, claim: Claim, lease_seconds: float | None = None) -> bool:
        ls = lease_seconds if lease_seconds is not None else globals()["lease_seconds"]()
        async with self._lock:
            run_id = str(claim)
            token = getattr(claim, "token", None)
            if self._tokens.get(run_id) != token:
                return False
            self._claims[run_id] = _now() + ls
            return True

    async def recover_expired(self) -> int:
        async with self._lock:
            now = _now()
            expired = [rid for rid, dl in self._claims.items() if dl <= now]
            n = 0
            for rid in expired:
                self._claims.pop(rid, None)
                self._tokens.pop(rid, None)
                # At-least-once: push back to ready (RPUSH, so it becomes next claimed)
                if rid not in self._ready and rid not in self._parked:
                    self._ready.append(rid)
                    n += 1
            return n

    async def depth(self) -> int:
        async with self._lock:
            return len(self._ready)

    async def parked_depth(self) -> int:
        async with self._lock:
            return len(self._parked)

    async def park(self, claim: Claim, code: str) -> bool:
        async with self._lock:
            run_id = str(claim)
            token = getattr(claim, "token", None)
            if self._tokens.get(run_id) != token:
                return False
            self._claims.pop(run_id, None)
            self._tokens.pop(run_id, None)
            self._parked[run_id] = (code, _now())
            return True

    # For tests: introspection
    async def claims_depth(self) -> int:
        async with self._lock:
            return len(self._claims)
