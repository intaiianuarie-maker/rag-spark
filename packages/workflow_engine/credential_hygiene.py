"""Credential-reference hygiene for every W4 surface (T-V4-020).

One rule, applied at every W4 boundary — the draft store (T-V4-017), the route
schemas (T-V4-019) and the migration draft (T-V4-018):

    credentials travel by REFERENCE (a vault id / a name), never by value.

A credential **value** (an API key, a password, a bearer token) is refused at
the boundary — never stored, never logged, never echoed back. A credential
**reference** (``vault:prod/api-key``, ``db-credential``) is the only accepted
form, and it is validated to *look* like a reference: short, structured, and
carrying no secret material of its own. A reference field is therefore not a
hole — ``api_key_ref: sk-live-…`` is refused, because that is a value wearing a
reference's name.

Where this module lives matters. It is deliberately **pure**: no store import,
no persistence, no I/O. That is what lets the route schemas (which must not
import the store per T-V4-019) and the store itself share exactly one
deny-list and one reference grammar, instead of two lists that drift apart.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

#: Deny-list keys matched **whole** (case-insensitive, normalised): a key that
#: *is* one of these is a credential key. Token splitting alone would miss
#: ``api_key`` (tokens ``api``/``key`` — and matching bare ``key`` would then
#: refuse ``sort_key``, so it must not be in the token set).
DENY_EXACT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "private_key",
        "password",
        "passwd",
        "secret",
        "token",
        "bearer",
        "authorization",
        "auth",
        "credential",
        "credentials",
    }
)

#: Deny-list tokens: a key carrying one of these *tokens* is a credential key,
#: so ``client_secret`` and ``auth_token`` are caught while ``author``,
#: ``tokenizer`` and ``template_key`` are not.
DENY_TOKENS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "bearer",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "apikey",
    }
)

#: Key suffixes that make a credential *reference-shaped* rather than
#: value-shaped. ``api_key_ref`` is a reference; ``api_key`` is a value.
REFERENCE_KEY_SUFFIXES = (
    "_ref",
    "-ref",
    "ref",
    "_id",
    "-id",
    "_name",
    "-name",
    "_key_id",
    "_keyid",
)

#: Secret-shaped patterns. The replacement must **drop** the match — a pattern
#: whose group re-emits the match would "redact" while leaking the material.
SECRET_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)sk-[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"(?i)gh[pous]_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"(?i)-----BEGIN [A-Z ]+-----"), "[REDACTED]"),
    # A Bearer scheme alone is not the secret, so keep it and drop the token.
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_\.]+"), r"\1[REDACTED]"),
)

#: A deny-listed key used as a value assignment, tolerant of JSON quoting. The
#: value group runs to the end of the line so a whole ``Bearer <token>`` — not
#: just its first word — is redacted.
DENY_ASSIGNMENT = re.compile(
    r"(?i)([\"']?"
    r"(api_key|apikey|password|passwd|secret|token|bearer|authorization|auth|"
    r"credential|credentials|private_key)"
    r"[\"']?\s*[:=]\s*)(.*)"
)

#: Bounds for a reference string.
MAX_REFERENCE_CHARS = 192
MIN_REFERENCE_CHARS = 1

#: ``vault:<path>`` — the explicit vault-addressable form.
_VAULT_REF = re.compile(r"^vault:[A-Za-z0-9_\-./]{1,191}$")

#: A bare reference name: short alphanumeric runs joined by separators. It
#: deliberately has no ``=``, no whitespace and no scheme separators.
_BARE_REF = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$")

#: Longest unbroken run allowed in a bare name. Credential material is a long
#: blob (32+ characters with no separator); names are short runs. Without this,
#: a short raw key would pass as a "name".
MAX_NAME_RUN_CHARS = 24

#: A credential **value**: rejected wherever it appears in a W4 payload.
_VALUE_SHAPED = re.compile(r"(?i)^.{0,4096}$")


class CredentialHygieneError(RuntimeError):
    """Base class for credential-hygiene refusals."""


class CredentialValueError(CredentialHygieneError):
    """A credential value was offered where a reference is required."""


class CredentialReferenceError(CredentialHygieneError):
    """A reference-shaped key carried something that is not a reference."""


# --------------------------------------------------------------------------- #
# Reference model
# --------------------------------------------------------------------------- #


class CredentialReference(BaseModel):
    """A credential reference: *where* a secret lives, never *what* it is.

    ``ref`` is validated against the reference grammar, so a reference cannot
    be used to smuggle a value.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["vault", "name"] = "vault"
    ref: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=MIN_REFERENCE_CHARS,
            max_length=MAX_REFERENCE_CHARS,
        ),
    ]

    @field_validator("ref")
    @classmethod
    def _must_look_like_a_reference(cls, value: str) -> str:
        """Enforce the grammar in the model, not only in ``is_reference``.

        Without this, ``parse()`` would accept any string of a plausible
        length — including a real API key that happens to be short enough.
        """
        if not cls.is_reference(value):
            raise ValueError(
                "credential reference must be vault:<path> or a short "
                "name; that value is not reference-shaped"
            )
        return value

    @classmethod
    def parse(cls, value: str) -> CredentialReference:
        """Parse *value* into a reference, refusing anything value-shaped."""
        cleaned = value.strip()
        kind: Literal["vault", "name"] = (
            "vault" if cleaned.startswith("vault:") else "name"
        )
        return cls(kind=kind, ref=cleaned)  # raises if not reference-shaped

    @staticmethod
    def is_reference(value: object) -> bool:
        """Whether *value* is shaped like a credential reference.

        A reference never carries secret-shaped material: ``sk-…`` and
        ``Bearer …`` are refused even though a short key is otherwise
        name-shaped. A bare name must also be short runs joined by separators,
        because credential material is an unbroken blob and a name is not.
        """
        if not isinstance(value, str):
            return False
        candidate = value.strip()
        if not (MIN_REFERENCE_CHARS <= len(candidate) <= MAX_REFERENCE_CHARS):
            return False
        if find_secret_text(candidate) is not None:
            return False
        if _VAULT_REF.match(candidate):
            return True
        if not _BARE_REF.match(candidate):
            return False
        runs = re.split(r"[._-]", candidate)
        return max((len(run) for run in runs), default=0) <= MAX_NAME_RUN_CHARS


# --------------------------------------------------------------------------- #
# Key / value classification
# --------------------------------------------------------------------------- #


def _tokens(key: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", key.lower()) if t]


def is_credential_key(key: str) -> bool:
    """Whether *key* names a credential (``api_key``, ``client_secret`` …).

    Matched on the whole normalised key first (``api_key``, ``private_key``),
    then on its tokens (``client_secret``, ``auth_token``). Bare ``key`` and
    bare ``auth``-adjacent words like ``tokenizer`` are deliberately not deny
    tokens on their own, so ordinary config keys survive.
    """
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if not normalized:
        return False
    if normalized in DENY_EXACT_KEYS:
        return True
    return any(token in DENY_TOKENS for token in normalized.split("_"))


def is_reference_key(key: str) -> bool:
    """Whether *key* is reference-shaped (``api_key_ref``, ``db_password_id`` …).

    Checked **before** :func:`is_credential_key`: a reference-shaped key is the
    sanctioned way to point at a credential, so it must not be treated as a
    credential value simply for containing a deny token.
    """
    lowered = key.lower()
    return any(lowered.endswith(suffix) for suffix in REFERENCE_KEY_SUFFIXES)


def find_secret_text(text: str) -> str | None:
    """Return the first secret-shaped span or deny-listed assignment in *text*."""
    for pattern, _ in SECRET_REWRITES:
        match = pattern.search(text)
        if match:
            return match.group(0)
    match = DENY_ASSIGNMENT.search(text)
    if match:
        return match.group(0)
    return None


def find_credential_value(config: Any) -> str | None:
    """Return the first credential **value** in *config*, or ``None``.

    A value is refused when a credential key carries material that is not a
    reference, or when any string anywhere in the config is secret-shaped.
    Reference-shaped keys carrying a valid reference are fine.
    """
    if isinstance(config, dict):
        for key, value in config.items():
            name = str(key)
            if isinstance(value, str):
                if is_reference_key(name):
                    if not CredentialReference.is_reference(value):
                        return f"{name}={value!r}"
                    # A reference must not itself carry secret material.
                    if find_secret_text(value):
                        return f"{name}={value!r}"
                    continue
                if is_credential_key(name):
                    return f"{name}={value!r}"
                hit = find_secret_text(value)
                if hit:
                    return f"{name}: {hit}"
                continue
            hit = find_credential_value(value)
            if hit:
                return hit
        return None
    if isinstance(config, (list, tuple)):
        for value in config:
            hit = find_credential_value(value)
            if hit:
                return hit
        return None
    if isinstance(config, str):
        return find_secret_text(config)
    return None


def assert_references_only(config: Any, *, where: str = "config") -> Any:
    """Refuse credential *values*; allow credential *references*.

    Raises :class:`CredentialValueError` (a value-shaped credential) or
    :class:`CredentialReferenceError` (a reference-shaped key carrying material
    that is not a reference).
    """
    hit = find_credential_value(config)
    if hit is None:
        return config
    key = hit.split("=", 1)[0]
    if is_reference_key(key):
        raise CredentialReferenceError(
            f"{where} carries a non-reference credential in {key!r}: "
            f"{hit[:48]!r}; pass a vault id/name instead of the value"
        )
    raise CredentialValueError(
        f"{where} carries a credential value in {key!r}: {hit[:48]!r}; "
        "pass a vault id/name instead of the value"
    )


def redact_text(text: Any) -> Any:
    """Redact secret material from a projection (never echoed, never logged)."""
    if not isinstance(text, str):
        return text
    match = DENY_ASSIGNMENT.search(text)
    if match:
        # Keep the key label, drop the value.
        redacted = text[: match.start()] + match.group(1) + "[REDACTED]" + text[match.end():]
    else:
        redacted = text
    for pattern, replacement in SECRET_REWRITES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


__all__ = [
    "DENY_ASSIGNMENT",
    "DENY_EXACT_KEYS",
    "DENY_TOKENS",
    "MAX_NAME_RUN_CHARS",
    "MAX_REFERENCE_CHARS",
    "SECRET_REWRITES",
    "CredentialHygieneError",
    "CredentialReference",
    "CredentialReferenceError",
    "CredentialValueError",
    "assert_references_only",
    "find_credential_value",
    "find_secret_text",
    "is_credential_key",
    "is_reference_key",
    "redact_text",
]
