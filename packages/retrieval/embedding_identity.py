"""Embedding identity: one schema, stamped on every write, verified before use.

T-R06. The review proved an asymmetry: the REBUILD path
(``scripts/reindex_all_chunks.py``) stamped provider/model/revision/dimension
onto every record, while NORMAL ingestion
(``apps/worker/jobs/ingest_pipeline.py``) omitted them entirely. So the same
collection could hold vectors from two different models with nothing on the
record to tell them apart, and a query provider of the wrong model or the wrong
dimension could be used against them without complaint.

This module is the single source of truth for that identity:

  * :func:`provider_identity` builds the SAME four keys the rebuild path uses,
    so both write paths produce byte-identical metadata;
  * :func:`merge_identity` applies identity AFTER caller metadata, so a caller
    cannot smuggle in ``embedding_model`` and pass it off as trusted;
  * :func:`verify_record_identity` refuses unknown or incompatible identity
    loudly. It never pads and never truncates a vector to make it fit — a
    dimension mismatch is a bug or an attack, not something to paper over.

Scope note (Phase 1): the verification functions here are complete and tested,
but WIRING them into ``qdrant_store.upsert``/``search`` belongs to the card
that owns the collection boundary (dev1's T-R05 lane, which has uncommitted
work in that file). See the T-R06 handoff row for the exact insertion point.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

#: The canonical identity keys. Order and spelling are load-bearing: the rebuild
#: path's ``provider_identity`` must produce exactly these, and a test asserts
#: that parity rather than trusting the two copies to stay aligned.
IDENTITY_KEYS: tuple[str, ...] = (
    "embedding_provider",
    "embedding_model",
    "embedding_revision",
    "embedding_dimension",
)


class EmbeddingIdentityError(ValueError):
    """Raised when recorded identity is absent, unknown or incompatible.

    A ``ValueError`` subclass because it is a bad-value condition, but it is
    NEVER swallowed by a caller: the whole point is that an incompatible
    provider fails loudly instead of silently corrupting a collection.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class EmbeddingIdentity:
    """The four facts that decide whether two vectors may be compared."""

    provider: str
    model: str
    revision: str
    dimension: int

    def as_metadata(self) -> dict[str, Any]:
        """The canonical metadata fragment, in :data:`IDENTITY_KEYS` order."""
        return {
            "embedding_provider": self.provider,
            "embedding_model": self.model,
            "embedding_revision": self.revision,
            "embedding_dimension": int(self.dimension),
        }

    def as_tuple(self) -> tuple[str, str, str, int]:
        return (self.provider, self.model, self.revision, int(self.dimension))

    def matches(self, other: EmbeddingIdentity) -> bool:
        """Provider, model, revision AND dimension must all agree.

        Revision counts: two records from the same model name but different
        pinned revisions are not comparable, and neither are the same model at
        two dimensions. All four fields or nothing.
        """
        return self.as_tuple() == other.as_tuple()

    def mismatch_reason(self, other: EmbeddingIdentity) -> str:
        """Name the specific field that disagrees, for a useful error message."""
        for field, mine, theirs in (
            ("embedding_provider", self.provider, other.provider),
            ("embedding_model", self.model, other.model),
            ("embedding_revision", self.revision, other.revision),
            ("embedding_dimension", self.dimension, other.dimension),
        ):
            if mine != theirs:
                return f"{field}: stored={theirs!r} requested={mine!r}"
        return "identity matches"


def provider_identity(provider: Any, observed_dim: int) -> EmbeddingIdentity:
    """Build identity from a live provider plus the dimension actually observed.

    ``observed_dim`` is measured from the vectors, not trusted from the
    provider's declared attribute: a provider that claims 1024 and returns 768
    is exactly the failure this module exists to catch, so the observed value
    is what gets recorded.
    """
    model = str(getattr(provider, "model_name", "") or "")
    revision = str(getattr(provider, "revision", "") or "")
    if not model:
        raise EmbeddingIdentityError(
            "provider exposes no model_name; refusing to write unidentifiable vectors"
        )
    if int(observed_dim) <= 0:
        raise EmbeddingIdentityError(
            f"refusing to record identity with dimension {observed_dim!r}"
        )
    return EmbeddingIdentity(
        provider=type(provider).__name__,
        model=model,
        revision=revision,
        dimension=int(observed_dim),
    )


def merge_identity(
    metadata: Mapping[str, Any] | None,
    identity: EmbeddingIdentity,
) -> dict[str, Any]:
    """Attach identity to caller metadata, with identity WINNING.

    Caller metadata is filtered first: any caller-supplied value under an
    identity key is DROPPED, not merged. Without this, a chunk whose metadata
    carried ``embedding_model`` could overwrite the real one and make a
    mismatched vector look correctly labelled.
    """
    merged = {
        key: value
        for key, value in dict(metadata or {}).items()
        if key not in IDENTITY_KEYS
    }
    merged.update(identity.as_metadata())
    return merged


def identity_from_metadata(metadata: Mapping[str, Any] | None) -> EmbeddingIdentity:
    """Read identity back off a record. Raises if any key is missing."""
    data = dict(metadata or {})
    missing = [key for key in IDENTITY_KEYS if key not in data]
    if missing:
        raise EmbeddingIdentityError(
            f"record carries no embedding identity (missing {missing}); it cannot be "
            "compared against a provider"
        )
    try:
        dimension = int(data["embedding_dimension"])
    except (TypeError, ValueError) as exc:
        raise EmbeddingIdentityError(
            f"embedding_dimension is not an integer: {data['embedding_dimension']!r}"
        ) from exc
    return EmbeddingIdentity(
        provider=str(data["embedding_provider"]),
        model=str(data["embedding_model"]),
        revision=str(data["embedding_revision"]),
        dimension=dimension,
    )


def verify_record_identity(
    metadata: Mapping[str, Any] | None,
    requested: EmbeddingIdentity,
    *,
    collection: str = "",
) -> EmbeddingIdentity:
    """Verify a WRITE record's identity against the provider being used.

    Raises :class:`EmbeddingIdentityError` when the record carries no identity,
    or carries an identity incompatible with ``requested``. Never adjusts a
    vector to fit.
    """
    stored = identity_from_metadata(metadata)
    if not stored.matches(requested):
        where = f" for collection {collection!r}" if collection else ""
        raise EmbeddingIdentityError(
            f"embedding identity mismatch{where}: {stored.mismatch_reason(requested)}"
        )
    return stored


def verify_query_identity(
    collection_identity: EmbeddingIdentity | None,
    requested: EmbeddingIdentity,
    *,
    collection: str = "",
) -> EmbeddingIdentity:
    """Verify a QUERY provider against the collection it will search.

    An UNKNOWN collection identity (None) is rejected rather than assumed
    compatible. Assuming would be the quiet failure this ticket exists to
    remove: a collection built by an older, unlabelled rebuild has no
    recorded identity, and "probably fine" is not a check.
    """
    if collection_identity is None:
        where = f" {collection!r}" if collection else ""
        raise EmbeddingIdentityError(
            f"collection{where} has no recorded embedding identity; refusing to query "
            "with an unverified provider"
        )
    if not collection_identity.matches(requested):
        where = f" {collection!r}" if collection else ""
        raise EmbeddingIdentityError(
            f"query provider incompatible with collection{where}: "
            f"{collection_identity.mismatch_reason(requested)}"
        )
    return collection_identity


def observed_dimension(vectors: Sequence[Sequence[float]]) -> int:
    """Dimension actually produced by a batch, or raise.

    Deliberately refuses to guess from the first vector when the batch is
    ragged, and refuses a zero-length batch.
    """
    if not vectors:
        raise EmbeddingIdentityError("no vectors returned; cannot determine dimension")
    widths = {len(vector) for vector in vectors}
    if len(widths) != 1:
        raise EmbeddingIdentityError(
            f"ragged embedding batch: vectors have mixed dimensions {sorted(widths)}"
        )
    width = widths.pop()
    if width <= 0:
        raise EmbeddingIdentityError("embedding batch produced zero-length vectors")
    return width


def audit_identity(
    records: Sequence[Mapping[str, Any]],
    requested: EmbeddingIdentity,
) -> dict[str, Any]:
    """Audit a whole batch of record metadata against one provider.

    Returns counts rather than raising on the first problem, so a rebuild can
    report exactly how many records are unlabelled and how many mismatch
    instead of dying on record one. Callers that need safety must still reject
    when ``incompatible`` or ``unlabelled`` is non-zero — this reports, it does
    not decide.
    """
    total = len(records)
    unlabelled = 0
    incompatible = 0
    reasons: dict[str, int] = {}
    for record in records:
        try:
            stored = identity_from_metadata(record)
        except EmbeddingIdentityError:
            unlabelled += 1
            continue
        if not stored.matches(requested):
            incompatible += 1
            reason = stored.mismatch_reason(requested).split(":")[0]
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "total": total,
        "labelled": total - unlabelled,
        "unlabelled": unlabelled,
        "incompatible": incompatible,
        "mismatch_reasons": reasons,
        "requested": requested.as_metadata(),
    }