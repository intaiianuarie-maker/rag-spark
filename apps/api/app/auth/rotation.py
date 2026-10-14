"""T-R01 (P0) — signing-secret rotation mechanism.

What a rotation must achieve, and how this module does it:

1. **Stop accepting tokens signed with the old secret.** The new secret becomes
   the process's ``AUTH_JWT_SECRET`` (and the settings cache is dropped, so
   every later mint/verify uses it).
2. **Stop accepting sessions that could still carry old tokens.** Every
   ``auth:session:*`` record is deleted, so any access token still in flight
   fails the liveness check that ``app/authz/principal.py`` performs — and, with
   it, the owner binding.
3. **Stop accepting refresh tokens.** Every ``auth:refresh:*`` and
   ``auth:family:*`` record is deleted, so no stale ``fid`` can be replayed
   against the new secret.

This is a **mechanism plus procedure**, deliberately *not* a new HTTP endpoint:
an unauthenticated-reachable "rotate the signing secret" route would be a
worse problem than the one it fixes. The coordinator/operator runs it directly:

    # 1. dry run — prints what WOULD be invalidated, changes nothing
    /root/ko-venv/bin/python3 -m app.auth.rotation --plan

    # 2. set the new secret in the process environment / .env FIRST
    export AUTH_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

    # 3. execute — revoke all sessions + refresh tokens, reload the secret
    /root/ko-venv/bin/python3 -m app.auth.rotation --execute

Step 3 must run on every API instance (and the new secret must be present in
compose's environment for all of them) before the old secret is discarded. The
procedure is deliberately not runnable against production by me: the caller
owns the deployment and the secret distribution.

Nothing here is exercised against live state by tests; they drive a fake Redis.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
from typing import Any

from app.auth.adapters import redis_sessions as _session_store
from app.auth.adapters.redis_store import (
    FAMILY_KEY_PREFIX,
    REFRESH_KEY_PREFIX,
    count_matching_keys,
    delete_matching_keys,
)
from app.auth.config import (
    MIN_JWT_SECRET_LENGTH,
    AuthSettings,
    get_auth_settings,
    signing_secret_problem,
)


@dataclasses.dataclass(frozen=True)
class RotationPlan:
    """What a rotation would invalidate right now (counted, not guessed)."""

    sessions: int
    refresh_tokens: int
    refresh_families: int

    @property
    def total_keys(self) -> int:
        return self.sessions + self.refresh_tokens + self.refresh_families

    def as_dict(self) -> dict[str, int]:
        return {
            "sessions": self.sessions,
            "refresh_tokens": self.refresh_tokens,
            "refresh_families": self.refresh_families,
            "total_keys": self.total_keys,
        }


@dataclasses.dataclass(frozen=True)
class RotationReport:
    """What the rotation actually did."""

    plan: RotationPlan
    sessions_revoked: int
    refresh_tokens_revoked: int
    refresh_families_revoked: int
    secret_swapped: bool
    executed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "sessions_revoked": self.sessions_revoked,
            "refresh_tokens_revoked": self.refresh_tokens_revoked,
            "refresh_families_revoked": self.refresh_families_revoked,
            "secret_swapped": self.secret_swapped,
            "plan": self.plan.as_dict(),
        }


async def plan_rotation() -> RotationPlan:
    """Count the sessions and refresh records a rotation would invalidate."""
    return RotationPlan(
        sessions=await _session_store.count_sessions(),
        refresh_tokens=await count_matching_keys(REFRESH_KEY_PREFIX),
        refresh_families=await count_matching_keys(FAMILY_KEY_PREFIX),
    )


async def revoke_all_auth_state() -> tuple[int, int, int]:
    """Invalidate every session and every refresh record; returns the counts."""
    sessions = await _session_store.delete_sessions(
        await _session_store.iter_session_ids()
    )
    refresh_tokens = await delete_matching_keys(REFRESH_KEY_PREFIX)
    families = await delete_matching_keys(FAMILY_KEY_PREFIX)
    return sessions, refresh_tokens, families


def apply_new_secret(secret: str) -> None:
    """Install ``secret`` as the process signing secret and drop the caches.

    The new value is validated by the same rules boot uses, so a rotation can
    never install a secret the service would refuse to start with.
    """
    problem = signing_secret_problem(secret)
    if problem:
        raise ValueError(f"refusing to rotate onto an unusable secret: {problem}")
    os.environ["AUTH_JWT_SECRET"] = secret
    get_auth_settings.cache_clear()
    # Prove the swapped secret is actually the one that will be used.
    settings: AuthSettings = get_auth_settings()
    if settings.jwt_secret != secret:  # pragma: no cover - defensive
        raise RuntimeError("signing secret swap did not take effect")


async def rotate_signing_secret(
    secret: str | None = None,
    *,
    execute: bool = True,
) -> RotationReport:
    """Plan (and optionally execute) a full rotation.

    ``execute=False`` is the dry run: the plan is counted and returned, and no
    key is touched and no secret is swapped.
    """
    plan = await plan_rotation()
    if not execute:
        return RotationReport(
            plan=plan,
            sessions_revoked=0,
            refresh_tokens_revoked=0,
            refresh_families_revoked=0,
            secret_swapped=False,
            executed=False,
        )
    new_secret = secret if secret is not None else os.environ.get("AUTH_JWT_SECRET", "")
    # Validate BEFORE revoking anything: a rotation that fails halfway through
    # because the new secret was bad would strand every user.
    problem = signing_secret_problem(new_secret)
    if problem:
        raise ValueError(f"refusing to rotate: {problem}")
    sessions, refresh_tokens, families = await revoke_all_auth_state()
    apply_new_secret(new_secret)
    return RotationReport(
        plan=plan,
        sessions_revoked=sessions,
        refresh_tokens_revoked=refresh_tokens,
        refresh_families_revoked=families,
        secret_swapped=True,
        executed=True,
    )


def describe_procedure() -> str:
    """The operator-facing runbook (printed by ``--help`` and ``main``)."""
    return "\n".join(
        [
            "T-R01 rotation procedure (coordinator/operator executes — not the agent):",
            "  1. plan:      python -m app.auth.rotation --plan",
            f"  2. new secret: export AUTH_JWT_SECRET=$(python -c "
            f"'import secrets; print(secrets.token_urlsafe(48))')  "
            f"(>= {MIN_JWT_SECRET_LENGTH} chars)",
            "  3. execute:   python -m app.auth.rotation --execute   "
            "(revokes all sessions + refresh tokens, swaps the secret)",
            "  4. deploy the new AUTH_JWT_SECRET to every API instance, then",
            "     discard the old secret. Every in-flight access token dies at "
            "step 3 because its session is gone.",
        ]
    )


async def _run(args: argparse.Namespace) -> int:
    if args.plan:
        plan = await plan_rotation()
        print(f"rotation plan: {plan.as_dict()}")
        print(describe_procedure())
        return 0
    if args.execute:
        report = await rotate_signing_secret(execute=True)
        print(f"rotation executed: {report.as_dict()}")
        return 0
    print(describe_procedure())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T-R01 signing-secret rotation")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true", help="dry run: count what would go")
    group.add_argument("--execute", action="store_true", help="revoke state and swap the secret")
    parser.add_argument(
        "--secret",
        default=None,
        help="new secret (default: $AUTH_JWT_SECRET). Never pass one on a shared shell.",
    )
    args = parser.parse_args(argv)
    if args.execute and args.secret:
        os.environ["AUTH_JWT_SECRET"] = args.secret
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())