"""Queued sync consumer with persisted cursors (T-V1-034).

Turns the connector set into a **resumable pipeline** instead of a rescan loop:

- **queued**: a sync job is claimed from a durable claim/ack queue (the
  T-V1-020 lease protocol, namespaced under ``sync:`` by
  ``connectors.adapters.sync_queue``), processed, and acked **only after** the
  outcome is durably persisted — a crash mid-job leaves the claim un-acked, the
  lease expires, redelivery resumes from the persisted cursor;
- **workspace-configured**: the sources to sync come from the workspace's
  connector configuration (:data:`load_workspace_sources`, env-JSON store
  shaped like T-V1-028's ``WORKSPACE_MODEL_CONFIG`);
- **behind adapter boundaries**: the consumer only speaks the
  :class:`~connectors.interfaces.Connector` contract plus three narrow seams —
  :class:`SyncProgressStore` (persistence), :class:`SyncQueue` (durable
  claim/ack) and an optional publisher callback. Vendor SDKs and Redis stay in
  ``connectors.adapters.*``;
- **persisted source identity + checksums**: each source is identified by
  ``(workspace_id, connector_id, kind, settings_fingerprint)``; a reconfigured
  source bumps its generation and drops the old cursor/checksums rather than
  resuming against the wrong content;
- **snapshot/keyset progress, never positional**: progress stores a keyset
  cursor (``k:<external_id>`` — see ``connectors.interfaces.page_items``) plus
  a digest of the listing snapshot the run started from. Offsets are never
  persisted: an offset shifts when the source changes between runs, which is
  how items get skipped;
- **item outcomes**: every item ends as ``created`` / ``updated`` /
  ``unchanged`` / ``failed`` (with a T-V1-025 canonical reason code), keyed by
  its sha256 checksum, so the next run can tell new from changed without
  re-reading anything it already read.

Durable store: :class:`connectors.adapters.progress_store.SqlAlchemySyncProgressStore`
(migration ``0008_connector_sync_progress``). :class:`InMemorySyncProgressStore`
implements the same protocol for tests and single-process development.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from core.errors import ReasonCode, normalize_code

from .interfaces import (
    Connector,
    ConnectorConfig,
    ConnectorKind,
    SourceItem,
    is_keyset_cursor,
    keyset_cursor,
)

logger = logging.getLogger("knowledge_os.connectors.sync")

#: Queue payload prefix. The queue entry IS the job identity (T-V1-026).
SYNC_JOB_PREFIX = "sync:"

#: Env-JSON workspace → connector configuration store (see
#: :func:`load_workspace_sources`).
WORKSPACE_CONNECTOR_CONFIG_ENV = "WORKSPACE_CONNECTOR_CONFIG"

#: Release decision (T-R09): V1 ships as upload-only; continuous sync scheduling
#: and automated worker consumption are M6-pending. QueuedSyncConsumer is available
#: for scoped/on-demand execution.
V1_SHIP_MODE = "upload-only"
M6_STATUS = "pending"

#: Safety cap on pages per run — a misbehaving adapter that returns the same
#: page forever must fail loudly instead of looping.
DEFAULT_MAX_PAGES = 10_000



class ItemStatus(StrEnum):
    """Terminal outcome of one item in one sync run (persisted per item)."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    DELETED = "deleted"


# ---- persisted shapes --------------------------------------------------------


def settings_fingerprint(settings: Mapping[str, Any]) -> str:
    """Stable digest of a connector's settings (part of the source identity)."""
    payload = json.dumps(settings, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Persisted identity of a synced source.

    The settings fingerprint is in the identity, not just a column: changing
    settings means it is a *different* source, so old progress and checksums
    must not be resumed against it (they describe content we never saw).
    """

    workspace_id: str
    connector_id: str
    kind: ConnectorKind
    settings_fingerprint: str

    @classmethod
    def from_config(cls, config: ConnectorConfig) -> SourceIdentity:
        return cls(
            workspace_id=config.workspace_id,
            connector_id=config.connector_id,
            kind=config.kind,
            settings_fingerprint=settings_fingerprint(config.settings),
        )

    @property
    def key(self) -> str:
        return source_key(self.workspace_id, self.connector_id)


def source_key(workspace_id: str, connector_id: str) -> str:
    return f"{workspace_id}/{connector_id}"


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    """One item's result: status + checksum + optional canonical failure code."""

    external_id: str
    status: ItemStatus
    checksum: str | None = None
    size_bytes: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    run_id: str | None = None
    occurred_at: datetime | None = None
    #: Source generation this outcome belongs to (bumped when settings change).
    generation: int = 1


@dataclass(slots=True)
class SyncProgress:
    """Durable progress for one source (one row, upserted per page)."""

    identity: SourceIdentity
    generation: int = 1
    #: Keyset cursor only — ``None`` means "start from the top of the listing".
    cursor: str | None = None
    #: Listing digest captured when the run started (snapshot progress).
    snapshot: str | None = None
    last_run_id: str | None = None
    items_seen: int = 0
    items_created: int = 0
    items_updated: int = 0
    items_unchanged: int = 0
    items_failed: int = 0
    items_deleted: int = 0
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class SyncReport:
    """What one run of one source did (also the structured-log payload)."""

    workspace_id: str
    connector_id: str
    kind: ConnectorKind
    run_id: str
    generation: int
    resumed: bool
    pages: int
    discovered: int
    created: int
    updated: int
    unchanged: int
    failed: int
    deleted: int = 0
    snapshot: str | None = None
    snapshot_drift: bool = False
    #: ``None`` when the run completed; otherwise the keyset cursor it stopped at.
    cursor: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QueueRunSummary:
    """Result of :meth:`QueuedSyncConsumer.run_once` for one claimed job."""

    job_id: str
    workspace_id: str
    connector_id: str | None
    acked: bool
    reports: list[SyncReport] = field(default_factory=list)


# ---- seams (adapter boundaries) ----------------------------------------------


@runtime_checkable
class SyncProgressStore(Protocol):
    """Where progress, checksums and item outcomes live (the migration's seam)."""

    async def load(self, identity: SourceIdentity) -> SyncProgress | None:
        """Return the persisted progress for this source, or ``None``."""

    async def save(self, progress: SyncProgress) -> None:
        """Upsert progress — called after **every page**, never only at the end."""

    async def record_outcome(self, identity: SourceIdentity, outcome: ItemOutcome) -> None:
        """Persist one item's outcome (idempotent: one row per item/generation)."""

    async def outcomes(
        self, identity: SourceIdentity, generation: int
    ) -> dict[str, ItemOutcome]:
        """Persisted outcomes for ``generation`` by ``external_id``.

        Generation-scoped: outcomes from a *previous* configuration of this
        source must never decide created/updated for the new one.
        """

    async def reset(self, identity: SourceIdentity) -> None:
        """Drop all progress + outcomes for a reconfigured source."""


@dataclass(frozen=True, slots=True)
class SyncClaim:
    """A claimed sync job: payload + the T-V1-020 lease fence token."""

    job_id: str
    token: str


@runtime_checkable
class SyncQueue(Protocol):
    """Durable claim/ack queue (T-V1-020 semantics, ``sync:`` namespace)."""

    async def claim(self, timeout_seconds: int = 1) -> SyncClaim | None:
        """Claim the next job under a lease, or ``None`` on timeout."""

    async def ack(self, claim: SyncClaim) -> bool:
        """Release the claim **after** the durable outcome is committed."""


#: Publisher seam: called only for items that changed (created/updated), before
#: their outcome is recorded, so a failed publish is retried by redelivery.
Publisher = Callable[[SourceIdentity, SourceItem, bytes, str], Awaitable[None]]

#: Workspace configuration seam (injectable for tests / a future DB store).
SourceLoader = Callable[..., Sequence[ConnectorConfig]]


def default_connector_factory(
    config: ConnectorConfig, *, page_size: int | None = None
) -> Connector:
    """Resolve the adapter for ``config.kind`` (single wiring point).

    Imported lazily so ``connectors.__init__`` may import this module later
    without an import cycle; ``page_size`` is how the consumer's paging
    preference reaches the adapter it never inspects.
    """
    from . import create_connector

    return create_connector(config, page_size=page_size)


# ---- job payload codec -------------------------------------------------------


def encode_sync_job(workspace_id: str, connector_id: str | None = None) -> str:
    """Build the queue payload for a sync job (``sync:<ws>[/<connector>]``)."""
    if not workspace_id:
        raise ValueError("sync job needs a workspace_id")
    payload = SYNC_JOB_PREFIX + workspace_id
    if connector_id:
        payload = f"{payload}/{connector_id}"
    return payload


def parse_sync_job(payload: str) -> tuple[str, str | None]:
    """Parse a sync job payload; anything else is rejected loudly.

    Loud rejection matters: this queue is shared infrastructure, and a
    consumer that quietly "interprets" a foreign payload could sync the wrong
    workspace.
    """
    if not payload.startswith(SYNC_JOB_PREFIX):
        raise ValueError(f"not a sync job payload: {payload!r}")
    rest = payload[len(SYNC_JOB_PREFIX) :]
    workspace_id, sep, connector_id = rest.partition("/")
    if not workspace_id:
        raise ValueError(f"sync job payload has no workspace: {payload!r}")
    if sep and not connector_id:
        raise ValueError(f"sync job payload has an empty connector id: {payload!r}")
    return workspace_id, (connector_id if sep else None)


# ---- workspace configuration -------------------------------------------------


def load_workspace_sources(
    workspace_id: str, *, env: Mapping[str, str] | None = None
) -> list[ConnectorConfig]:
    """Load a workspace's configured connectors from the env-JSON store.

    ``WORKSPACE_CONNECTOR_CONFIG`` shape (same honest-v1 store style as
    T-V1-028's ``WORKSPACE_MODEL_CONFIG``):

    ``{"ws-a": [{"connector_id": "docs", "kind": "s3",
    "settings": {"bucket": "b", "prefix": "docs/"}}]}``

    Failure policy (disclosed, deliberately fail-open like T-V1-028): an
    absent store, invalid JSON or a malformed entry yields no sources (logged),
    never an exception that would take the sync loop down; an unknown ``kind``
    is skipped loudly so the misconfiguration stays visible.
    """
    source = env if env is not None else os.environ
    raw = source.get(WORKSPACE_CONNECTOR_CONFIG_ENV)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("workspace_connector_config_unparseable")
        return []
    if not isinstance(data, dict):
        logger.warning("workspace_connector_config_not_an_object")
        return []
    entries = data.get(workspace_id)
    if entries is None:
        return []
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        logger.warning(
            "workspace_connector_config_bad_entry",
            extra={"workspace_id": workspace_id},
        )
        return []
    configs: list[ConnectorConfig] = []
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning(
                "workspace_connector_config_bad_entry",
                extra={"workspace_id": workspace_id},
            )
            continue
        try:
            configs.append(
                ConnectorConfig(
                    workspace_id=workspace_id,
                    connector_id=str(entry["connector_id"]),
                    kind=ConnectorKind(str(entry["kind"])),
                    settings=dict(entry.get("settings") or {}),
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "workspace_connector_config_bad_entry",
                extra={"workspace_id": workspace_id, "reason": str(exc)},
            )
    return configs


# ---- stores ------------------------------------------------------------------


class InMemorySyncProgressStore:
    """Dict-backed :class:`SyncProgressStore` (tests / single-process dev).

    Not durable across a *process* restart by design — pass the same ``state``
    mapping to a second instance to simulate one (that is exactly what the
    restart tests do). Production durability is
    ``connectors.adapters.progress_store.SqlAlchemySyncProgressStore``.
    """

    def __init__(self, state: dict[str, dict[str, Any]] | None = None) -> None:
        self._state: dict[str, dict[str, Any]] = state if state is not None else {}

    def _bucket(self, identity: SourceIdentity) -> dict[str, Any]:
        return self._state.setdefault(
            identity.key, {"progress": None, "outcomes": {}}
        )

    async def load(self, identity: SourceIdentity) -> SyncProgress | None:
        bucket = self._state.get(identity.key)
        if not bucket:
            return None
        return bucket.get("progress")

    async def save(self, progress: SyncProgress) -> None:
        progress.updated_at = datetime.now(UTC)
        self._bucket(progress.identity)["progress"] = progress

    async def record_outcome(self, identity: SourceIdentity, outcome: ItemOutcome) -> None:
        self._bucket(identity)["outcomes"][outcome.external_id] = outcome

    async def outcomes(
        self, identity: SourceIdentity, generation: int
    ) -> dict[str, ItemOutcome]:
        bucket = self._state.get(identity.key)
        if not bucket:
            return {}
        return {
            external_id: outcome
            for external_id, outcome in bucket["outcomes"].items()
            if outcome.generation == generation
        }

    async def reset(self, identity: SourceIdentity) -> None:
        self._state.pop(identity.key, None)


# ---- the consumer -------------------------------------------------------------


class QueuedSyncConsumer:
    """Runs queued sync jobs against workspace-configured connectors.

    One job = one workspace (optionally one connector). Every page is persisted
    before the next is fetched, so a restart continues from the keyset cursor
    instead of rescanning the listing from offset 0.
    """

    def __init__(
        self,
        *,
        store: SyncProgressStore,
        queue: SyncQueue,
        connector_factory: Callable[[ConnectorConfig], Connector] | None = None,
        load_sources: SourceLoader = load_workspace_sources,
        publisher: Publisher | None = None,
        page_size: int = 100,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        if max_pages < 1:
            raise ValueError(f"max_pages must be >= 1, got {max_pages}")
        self._store = store
        self._queue = queue
        self._connector_factory = connector_factory
        self._load_sources = load_sources
        self._publisher = publisher
        self._page_size = page_size
        self._max_pages = max_pages

    def _connector(self, config: ConnectorConfig) -> Connector:
        if self._connector_factory is not None:
            return self._connector_factory(config)
        return default_connector_factory(config, page_size=self._page_size)

    # -- queued entry point ---------------------------------------------------

    async def run_once(self, *, timeout_seconds: int = 1) -> QueueRunSummary | None:
        """Claim one sync job, run it, ack **after** the durable outcome.

        The claim is deliberately NOT released when processing raises: with
        T-V1-020 semantics the lease then expires and the job is redelivered,
        and the retry resumes from the persisted keyset cursor — finished pages
        are never redone, unfinished ones are (at-least-once, which is safe
        because publication is idempotent).
        """
        claim = await self._queue.claim(timeout_seconds)
        if claim is None:
            return None
        workspace_id, connector_id = parse_sync_job(claim.job_id)
        try:
            configs = list(self._load_sources(workspace_id))
            if connector_id is not None:
                configs = [c for c in configs if c.connector_id == connector_id]
            if connector_id is not None and not configs:
                logger.warning(
                    "sync_source_not_configured",
                    extra={"workspace_id": workspace_id, "connector_id": connector_id},
                )
            reports = [await self.run_sync(config) for config in configs]
        except Exception:
            logger.exception(
                "sync_job_failed",
                extra={"job_id": claim.job_id, "workspace_id": workspace_id},
            )
            raise
        summary = QueueRunSummary(
            job_id=claim.job_id,
            workspace_id=workspace_id,
            connector_id=connector_id,
            acked=await self._queue.ack(claim),
            reports=reports,
        )
        return summary

    # -- one source ------------------------------------------------------------

    async def run_sync(self, config: ConnectorConfig) -> SyncReport:
        """Sync one connector source from its persisted keyset cursor."""
        identity = SourceIdentity.from_config(config)
        progress = await self._store.load(identity)
        if progress is None:
            progress = SyncProgress(identity=identity)
        elif progress.identity.settings_fingerprint != identity.settings_fingerprint:
            # Reconfigured source: old cursor/checksums describe different
            # settings. Drop them and start a new generation.
            await self._store.reset(identity)
            progress = SyncProgress(identity=identity, generation=progress.generation + 1)
            logger.info(
                "sync_source_reconfigured",
                extra={
                    "workspace_id": identity.workspace_id,
                    "connector_id": identity.connector_id,
                    "generation": progress.generation,
                },
            )
        if progress.cursor is not None and not is_keyset_cursor(progress.cursor):
            # A persisted offset would shift under source churn; refuse rather
            # than silently resume at the wrong item (never re-teach the loop
            # to be positional).
            raise ValueError(
                f"persisted cursor is not a keyset cursor: {progress.cursor!r}"
            )

        resumed = progress.cursor is not None
        run_id = uuid.uuid4().hex
        if not resumed:
            progress.cursor = None
            progress.snapshot = None
            progress.items_seen = 0
            progress.items_created = 0
            progress.items_updated = 0
            progress.items_unchanged = 0
            progress.items_failed = 0

        previous = await self._store.outcomes(identity, progress.generation)
        connector = self._connector(config)
        report = SyncReport(
            workspace_id=identity.workspace_id,
            connector_id=identity.connector_id,
            kind=identity.kind,
            run_id=run_id,
            generation=progress.generation,
            resumed=resumed,
            pages=0,
            discovered=0,
            created=0,
            updated=0,
            unchanged=0,
            failed=0,
            cursor=progress.cursor,
        )

        cursor = progress.cursor
        for _page in range(self._max_pages):
            discovery = await connector.discover(config, cursor=cursor)
            if progress.snapshot is None:
                progress.snapshot = discovery.snapshot
                report.snapshot = progress.snapshot
            elif discovery.snapshot and discovery.snapshot != progress.snapshot:
                # The listing changed while this run was in flight (or while
                # it was interrupted). Keyset paging still covers it without a
                # rescan; disclose the drift instead of pretending it's the
                # same snapshot.
                report.snapshot_drift = True
            if not discovery.items:
                # Nothing left after the cursor (source emptied out while the
                # run was interrupted): clear it, otherwise the stale keyset
                # would be re-read forever.
                progress.cursor = None
                progress.snapshot = None
                report.cursor = None
                break
            report.pages += 1
            for item in discovery.items:
                report.discovered += 1
                progress.items_seen += 1
                outcome = await self._process_item(
                    identity,
                    progress.generation,
                    run_id,
                    config,
                    connector,
                    item,
                    previous,
                )
                await self._store.record_outcome(identity, outcome)
                previous[item.external_id] = outcome
                if outcome.status is ItemStatus.CREATED:
                    report.created += 1
                    progress.items_created += 1
                elif outcome.status is ItemStatus.UPDATED:
                    report.updated += 1
                    progress.items_updated += 1
                elif outcome.status is ItemStatus.UNCHANGED:
                    report.unchanged += 1
                    progress.items_unchanged += 1
                else:
                    report.failed += 1
                    progress.items_failed += 1
                    report.errors.append(f"{item.external_id}: {outcome.error_code}")

            if discovery.next_cursor is None:
                # Run complete: clear the cursor so the *next* run starts a
                # fresh full listing (incremental detection is the checksums).
                progress.cursor = None
                progress.snapshot = None
                report.cursor = None
                break
            next_cursor = keyset_cursor(discovery.items[-1].external_id)
            if next_cursor == cursor:
                raise RuntimeError(
                    "sync cursor did not advance — adapter is not honouring the "
                    "keyset contract (positional rescan risk)"
                )
            cursor = next_cursor
            progress.cursor = cursor
            progress.last_run_id = run_id
            report.cursor = cursor
            await self._store.save(progress)  # durable BEFORE the next page
        else:
            raise RuntimeError(
                f"sync exceeded max_pages={self._max_pages} without finishing"
            )

        progress.last_run_id = run_id
        await self._store.save(progress)
        logger.info(
            "sync_run_finished",
            extra={
                "workspace_id": identity.workspace_id,
                "connector_id": identity.connector_id,
                "run_id": run_id,
                "generation": progress.generation,
                "pages": report.pages,
                "discovered": report.discovered,
                "items_created": report.created,
                "items_updated": report.updated,
                "items_unchanged": report.unchanged,
                "items_failed": report.failed,
                "resumed": report.resumed,
                "snapshot_drift": report.snapshot_drift,
            },
        )
        return report

    # -- one item ---------------------------------------------------------------

    async def _process_item(
        self,
        identity: SourceIdentity,
        generation: int,
        run_id: str,
        config: ConnectorConfig,
        connector: Connector,
        item: SourceItem,
        previous: Mapping[str, ItemOutcome],
    ) -> ItemOutcome:
        """Fetch/checksum one item and classify it against the persisted outcome."""
        now = datetime.now(UTC)
        prior = previous.get(item.external_id)
        advertised = item.metadata.get("checksum")
        advertised = advertised if isinstance(advertised, str) and len(advertised) == 64 else None
        if advertised is not None and prior is not None and prior.checksum == advertised:
            # Adapter already told us the bytes are identical (its own scan
            # checksum) and we recorded that exact checksum → unchanged, no
            # second download. Anything we cannot prove is fetched again.
            return ItemOutcome(
                external_id=item.external_id,
                status=ItemStatus.UNCHANGED,
                checksum=advertised,
                size_bytes=item.size_bytes,
                run_id=run_id,
                occurred_at=now,
                generation=generation,
            )
        try:
            content = await connector.fetch(config, item)
        except Exception as exc:
            return ItemOutcome(
                external_id=item.external_id,
                status=ItemStatus.FAILED,
                checksum=None,
                size_bytes=item.size_bytes,
                # T-V1-025: persist a canonical registry code, not a raw string.
                error_code=normalize_code(ReasonCode.INGESTION_ERROR),
                error_message=str(exc)[:500],
                run_id=run_id,
                occurred_at=now,
                generation=generation,
            )
        checksum = hashlib.sha256(content).hexdigest()
        if prior is None:
            status = ItemStatus.CREATED
        elif prior.checksum != checksum:
            status = ItemStatus.UPDATED
        else:
            status = ItemStatus.UNCHANGED
        if status in (ItemStatus.CREATED, ItemStatus.UPDATED) and self._publisher is not None:
            # Publish BEFORE recording: if publishing raises, no outcome is
            # stored and this page is redone on redelivery (at-least-once).
            await self._publisher(identity, item, content, checksum)
        return ItemOutcome(
            external_id=item.external_id,
            status=status,
            checksum=checksum,
            size_bytes=len(content),
            run_id=run_id,
            occurred_at=now,
            generation=generation,
        )
