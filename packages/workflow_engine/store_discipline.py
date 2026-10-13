"""Store discipline for workflow stores (T-V4-017 / W4).

The same discipline the T-V4-016 checkpoint store and the V3 ``StepStore``
enforce: payloads are **bounded** and secrets are handled **at the store
boundary**, never persisted.

Where the redacted surface lives
-------------------------------
``redact`` applies to a store's *projections* (summaries, journal lines) —
never to the revision payload itself. Redacting a revision would change its
canonical bytes and therefore its ``content_hash``, which is the record's
identity: a store that rewrote content on the way in could never hand back
the revision the compiler hashed. So this module offers three separately
safe primitives:

* :func:`redact` — for bounded summary/journal text;
* :func:`enforce_bounds` — refuse an oversized payload (never truncate:
  a truncated definition is a different definition than the one hashed);
* :func:`assert_no_secret_config` — refuse to persist node config carrying a
  deny-listed key or a secret-shaped value (fail closed, content untouched).

This module is intentionally standalone: ``checkpoint.py`` (T-V4-016) is
already committed and out of this ticket's allowlist, so it keeps its own
private copy. A follow-up may unify the two.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .interfaces import WorkflowDefinition

# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #

#: Maximum size of one serialized store payload (bytes of canonical JSON).
MAX_PAYLOAD_BYTES = 262_144  # 256 KB
#: Maximum length of a single redacted text projection.
MAX_TEXT_CHARS = 8_192
#: Maximum length of a revision summary projection.
MAX_SUMMARY_CHARS = 1_024
#: Maximum number of nodes named in a revision summary before it is capped.
MAX_SUMMARY_NODES = 64

# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #

#: Deny-list key *tokens* (case-insensitive). Matched against the tokens of a
#: config key, so ``authorization``, ``http_authorization`` and ``auth_token``
#: all count while ``author`` and ``tokenizer`` do not.
_DENY_KEY_TOKENS = frozenset(
    {
        "api_key",
        "apikey",
        "password",
        "passwd",
        "secret",
        "token",
        "bearer",
        "authorization",
        "auth",
    }
)

#: Secret-shaped rewrites as ``(pattern, replacement)`` pairs. The replacement
#: must **drop** the matched secret — a pattern with a capture group that
#: re-emits the match would "redact" while leaking the material.
_SECRET_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)sk-[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"(?i)gh[pous]_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"(?i)-----BEGIN [A-Z ]+-----"), "[REDACTED]"),
    # A Bearer scheme alone is not the secret, so keep it and drop the token.
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_\.]+"), r"\1[REDACTED]"),
)

#: A deny-listed key used as a value assignment. The optional quotes let it
#: match both free text (``Authorization: Bearer x``) and canonical JSON
#: (``"authorization": "Bearer x"``).
_DENY_ASSIGNMENT = re.compile(
    r"(?i)([\"']?"
    r"(api_key|apikey|password|passwd|secret|token|bearer|authorization|auth)"
    r"[\"']?\s*[:=]\s*)(\S+)"
)


class StoreDisciplineError(RuntimeError):
    """Base class for store-boundary refusals."""


class StoreBoundsError(StoreDisciplineError):
    """A payload exceeded the store's size bound."""


class StoreSecretError(StoreDisciplineError):
    """A payload carried a secret that must never reach the store."""


def canonical_bytes(spec: WorkflowDefinition) -> str:
    """Deterministic canonical JSON rendering of *spec*.

    Used for size limits and secret scanning. The **hash** is computed by
    ``WorkflowRevision.create`` (T-V4-008), which is the process-independent
    canonicalization the compiler verifies; this rendering is only needed for
    order-insensitive uses (a size cap, a membership scan).
    """
    return json.dumps(spec.model_dump(mode="json"), sort_keys=True, default=str)


def enforce_bounds(payload: str, *, limit: int = MAX_PAYLOAD_BYTES) -> str:
    """Return *payload* if within *limit*, else refuse.

    Oversized payloads are never silently truncated: a truncated definition
    is not the definition that was hashed, so a resume/publish would run a
    different workflow than the one approved.
    """
    size = len(payload.encode("utf-8"))
    if size > limit:
        raise StoreBoundsError(
            f"store payload of {size} bytes exceeds the {limit}-byte bound"
        )
    return payload


def _is_deny_key(key: str) -> bool:
    """Whether *key* carries a deny-listed token (``auth_token`` does)."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", key.lower()) if t]
    return any(t in _DENY_KEY_TOKENS for t in tokens)


def find_secret(text: str) -> str | None:
    """Return the first secret-shaped span or deny-listed assignment in *text*."""
    for pattern, _ in _SECRET_REWRITES:
        match = pattern.search(text)
        if match:
            return match.group(0)
    match = _DENY_ASSIGNMENT.search(text)
    if match:
        return match.group(0)
    return None


def find_secret_in_config(config: Any) -> str | None:
    """Return the first secret in *config*, or ``None``.

    Scans config **keys** for deny-listed tokens and string **values** for
    secret-shaped material. Node ids, names and descriptions are not secret
    channels, so a node named ``auth_check`` is not refused.
    """
    if isinstance(config, dict):
        for key, value in config.items():
            if _is_deny_key(str(key)):
                return f"{key}=…"
            hit = find_secret_in_config(value)
            if hit:
                return hit
        return None
    if isinstance(config, (list, tuple)):
        for value in config:
            hit = find_secret_in_config(value)
            if hit:
                return hit
        return None
    if isinstance(config, str):
        return find_secret(config)
    return None


def assert_no_secret_config(spec: WorkflowDefinition) -> WorkflowDefinition:
    """Refuse a definition whose node config carries a secret (fail closed).

    Only ``node.config`` is scanned: config is where credentials actually
    live (HTTP headers, API keys, tool tokens). Node ids, names and
    descriptions are not secret channels, so a node legitimately named
    ``auth_check`` is not refused for containing the word "auth".
    """
    for node in spec.nodes or []:
        hit = find_secret_in_config(node.config)
        if hit:
            raise StoreSecretError(
                f"node {node.id!r} config carries a secret ({hit[:24]}); "
                "refusing to persist it in the draft store"
            )
    return spec


def redact_text(text: Any) -> Any:
    """Redact secrets from *text* (deny-listed assignments + patterns)."""
    if not isinstance(text, str):
        return text
    redacted = text[:MAX_TEXT_CHARS]
    match = _DENY_ASSIGNMENT.search(redacted)
    if match:
        # Keep the key label (useful when auditing a summary), drop the value.
        redacted = (
            redacted[: match.start()] + match.group(1) + "[REDACTED]"
            + redacted[match.end():]
        )
    for pattern, replacement in _SECRET_REWRITES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact(value: Any) -> Any:
    """Recursively redact a JSON-ish projection before it is stored."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def build_summary(spec: WorkflowDefinition, *, description: str = "") -> str:
    """Build a bounded, redacted one-line projection of a definition.

    This is the store's redacted surface: what a history listing shows
    without echoing configuration or credentials.
    """
    nodes = list(spec.nodes or [])
    named = ", ".join(
        f"{n.type.value}:{n.id}" for n in nodes[:MAX_SUMMARY_NODES]
    )
    if len(nodes) > MAX_SUMMARY_NODES:
        named += f", …(+{len(nodes) - MAX_SUMMARY_NODES} more)"
    text = (
        f"{spec.id} · {len(nodes)} node(s)"
        f" [{named or 'none'}] · {len(spec.edges or [])} edge(s)"
    )
    if description:
        text += f" · {description[:MAX_SUMMARY_CHARS // 4]}"
    return str(redact_text(text))[:MAX_SUMMARY_CHARS]
