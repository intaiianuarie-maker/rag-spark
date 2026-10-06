"""Password hashing via argon2-cffi (argon2id).

``verify_password`` relies on argon2's constant-time verification; no custom
comparison or cryptography is implemented. Hashes are never logged or returned.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a plaintext password (argon2id, library defaults)."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time password verification.

    Returns ``False`` for any verification failure (mismatch or malformed hash)
    instead of raising, so callers always take the same generic error path.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether the stored hash uses outdated argon2 parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True
