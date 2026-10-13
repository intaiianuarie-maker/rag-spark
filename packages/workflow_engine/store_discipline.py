"""Store discipline for workflow stores (T-V4-017 / W4, hardened by T-V4-020).

The same discipline the T-V4-016 checkpoint store and the V3 ``StepStore``
enforce: payloads are **bounded** and credentials are handled **at the store
boundary**, never persisted.

Credential rules live in :mod:`credential_hygiene` — a single, pure source of
truth shared with the route schemas (T-V4-019), so the store and the API cannot
drift into denying different things. This module adds only what a *store* needs
on top: bounds, canonical rendering, redacted projections and summaries.

Where the redacted surface lives
-------------------------------
``redact`` applies to a store's *projections* (summaries, journal lines) —
never to the revision payload itself. Redacting a revision would change its
canonical bytes and therefore its ``content_hash``, which is the record's
identity: a store that rewrote content on the way in could never hand back
the revision the compiler hashed. So this module offers separately safe
primitives:

* :func:`redact` — for bounded summary/journal text;
* :func:`enforce_bounds` — refuse an oversized payload (never truncate:
  a truncated definition is a different definition than the one hashed);
* :func:`assert_no_secret_config` — refuse to persist node config carrying a
  credential **value** (reference forms are accepted and validated);
* :func:`assert_references_only` — the same rule, for a caller holding raw
  config rather than a definition.

This module is intentionally standalone with respect to ``checkpoint.py``
(T-V4-016): that file is committed and outside this ticket's allowlist, so it
keeps its own private copy of the deny-list. A follow-up may unify the two.
"""

from __future__ import annotations

import json
from typing import Any

from .credential_hygiene import (
    CredentialReferenceError,
    CredentialValueError,
    assert_references_only,
    find_credential_value,
    redact_text,
)
from .interfaces import WorkflowDefinition

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "MAX_SUMMARY_CHARS",
    "MAX_SUMMARY_NODES",
    "MAX_TEXT_CHARS",
    "StoreBoundsError",
    "StoreDisciplineError",
    "StoreSecretError",
    "assert_no_secret_config",
    "assert_references_only",
    "build_summary",
    "canonical_bytes",
    "enforce_bounds",
    "find_credential_value",
    "redact",
    "redact_text",
]

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


class StoreDisciplineError(RuntimeError):
    """Base class for store-boundary refusals."""


class StoreBoundsError(StoreDisciplineError):
    """A payload exceeded the store's size bound."""


class StoreSecretError(CredentialValueError, StoreDisciplineError):
    """A payload carried a credential value that must never reach the store.

    Inherits :class:`CredentialValueError` so a caller that screens every W4
    boundary the same way can catch one class; keeps its historical name
    because the T-V4-017 suite and the handler contract already use it.
    """


def canonical_bytes(spec: WorkflowDefinition) -> str:
    """Deterministic canonical JSON rendering of *spec*.

    Used for size limits and credential scanning. The **hash** is computed by
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


def assert_no_secret_config(spec: WorkflowDefinition) -> WorkflowDefinition:
    """Refuse a definition whose node config carries a credential value.

    Only ``node.config`` is scanned: config is where credentials actually live
    (HTTP headers, API keys, tool tokens). Node ids, names and descriptions are
    not credential channels, so a node legitimately named ``auth_check`` is not
    refused for containing the word "auth".

    Reference-shaped keys (``api_key_ref``, ``db_password_id``) are accepted
    provided they carry a reference rather than a value — see
    :mod:`credential_hygiene`.
    """
    for node in spec.nodes or []:
        try:
            assert_references_only(node.config, where=f"node {node.id!r} config")
        except CredentialValueError as exc:
            raise StoreSecretError(
                f"node {node.id!r} config carries a credential "
                f"({str(exc)[:64]}); refusing to persist it in the draft store"
            ) from exc
        except CredentialReferenceError as exc:
            raise StoreSecretError(
                f"node {node.id!r} config carries a malformed credential "
                f"reference ({str(exc)[:64]}); refusing to persist it"
            ) from exc
    return spec


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
    named = ", ".join(f"{n.type.value}:{n.id}" for n in nodes[:MAX_SUMMARY_NODES])
    if len(nodes) > MAX_SUMMARY_NODES:
        named += f", …(+{len(nodes) - MAX_SUMMARY_NODES} more)"
    text = (
        f"{spec.id} · {len(nodes)} node(s)"
        f" [{named or 'none'}] · {len(spec.edges or [])} edge(s)"
    )
    if description:
        text += f" · {description[:MAX_SUMMARY_CHARS // 4]}"
    return str(redact_text(text))[:MAX_SUMMARY_CHARS]

