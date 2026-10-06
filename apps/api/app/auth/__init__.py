"""Authentication module (M1-001a).

Owns user registration, password hashing, JWT access/refresh tokens, login
rate limiting, and logout invalidation. Workspaces/roles/API keys/sessions are
later tickets (M1-001b/c).

Reuse-first:
- password hashing: ``argon2-cffi`` (MIT) — argon2id, constant-time verify
- tokens: ``PyJWT`` (MIT) — HS256, issuer/audience enforced
- rate-limit + refresh-token state: Redis (already selected) behind
  ``adapters/redis_store.py`` so the vendor client stays isolated.
No custom cryptography is implemented.
"""

from __future__ import annotations

from app.auth.router import router

__all__ = ["router"]
