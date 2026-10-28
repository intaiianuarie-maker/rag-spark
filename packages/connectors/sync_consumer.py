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
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
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
    #: Durable source-level failure markers (root review): the discovery
    #: failure that happens BEFORE any ItemOutcome (unreachable host, bad
    #: credentials) used to be log-only, so the UI polled forever. Persisted
    #: here, reset on success, so a run start/success/failure is durable.
    last_error_code: str | None = None
    last_finished_at: datetime | None = None
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

#: Deleter seam: called for items present in a previous run but absent from the
#: current full listing, so their vectors/FTS rows can be tombstoned. Receives
#: (identity, external_id, prior_outcome). Deletion propagation is best-effort;
#: failures are recorded as ItemStatus.FAILED outcomes and retried on redelivery.
Deleter = Callable[[SourceIdentity, str, ItemOutcome], Awaitable[None]]

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
        deleter: Deleter | None = None,
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
        self._deleter = deleter
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
            reports = []
            for config in configs:
                report = await self.run_sync(config)
                if report.resumed:
                    # The resumed suffix cannot prove full-list membership or
                    # retry failed earlier pages. Reconcile once from the top
                    # under THIS claim before retirement, with no new enqueue.
                    report = await self.run_sync(config)
                reports.append(report)
        except Exception as exc:
            logger.warning(
                "sync_job_failed",
                extra={"job_id": claim.job_id, "workspace_id": workspace_id,
                       "error_type": type(exc).__name__},
            )
            raise
        # Ack only a CLEAN run — where "clean" includes the PERSISTED state,
        # not just this run's counters (root review): a resumed run's report
        # starts at zero, so an earlier page's FAILED item would otherwise be
        # acked unpublished. run_sync recomputes report.failed from the
        # generation's persisted outcomes at completion, which is what makes
        # this check carry failures across resume/retry.
        clean = all(report.failed == 0 for report in reports)
        summary = QueueRunSummary(
            job_id=claim.job_id,
            workspace_id=workspace_id,
            connector_id=connector_id,
            acked=await self._queue.ack(claim) if clean else False,
            reports=reports,
        )
        return summary

    # -- one source ------------------------------------------------------------

    async def run_sync(self, config: ConnectorConfig) -> SyncReport:
        """Run a traversal and persist safe terminal failures at source level."""
        try:
            return await self._run_sync(config)
        except Exception:
            # Fetch, publication and discovery failures all need an observable
            # source outcome, including failures before any item existed.
            identity = SourceIdentity.from_config(config)
            try:
                progress = await self._store.load(identity)
                if progress is not None:
                    progress.last_error_code = normalize_code(ReasonCode.INGESTION_ERROR)
                    progress.last_finished_at = datetime.now(UTC)
                    await self._store.save(progress)
            except Exception as exc:
                logger.warning("sync_failure_save_failed", extra={"error_type": type(exc).__name__})
            raise

    async def _run_sync(self, config: ConnectorConfig) -> SyncReport:
        """Traverse from the durable keyset; completed pages are not republished."""
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
        progress.last_attempt_at = datetime.now(UTC)
        if not resumed:
            progress.cursor = None
            progress.snapshot = None
            progress.items_seen = 0
            progress.items_created = 0
            progress.items_updated = 0
            progress.items_unchanged = 0
            progress.items_failed = 0
            progress.items_deleted = 0

        # Durable attempt identity (root review): persist the attempt BEFORE
        # the first discover so an unreachable host / bad credentials / crash
        # still records that an attempt happened, and the API can correlate
        # the current run via last_run_id (no client-clock comparisons).
        progress.last_run_id = run_id
        progress.last_error_code = None
        progress.last_finished_at = None
        try:
            await self._store.save(progress)
        except Exception:
            # A store failure at attempt time is fatal for this run: no
            # durable progress means no safe retry state.
            logger.warning(
                "sync_attempt_save_failed",
                extra={
                    "workspace_id": identity.workspace_id,
                    "connector_id": identity.connector_id,
                },
            )
            raise
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
        seen_ids: set[str] = set()
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
                # Nothing left after the cursor — the listing is exhausted
                # (a source that emptied out lands here too). The deletion
                # sweep runs on the SAME completion path as a final page
                # (root review: an empty listing must delete documents, not
                # break before the sweep). The cursor is cleared so the stale
                # keyset is not re-read forever.
                await self._sweep_deletions(
                    identity, progress, report, previous, seen_ids, run_id, resumed
                )
                progress.cursor = None
                progress.snapshot = None
                report.cursor = None
                break
            report.pages += 1
            for item in discovery.items:
                seen_ids.add(item.external_id)
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
                # Listing exhausted on a final page: the same completion path
                # as the empty-page branch (sweep + clear + success stamp).
                await self._sweep_deletions(
                    identity, progress, report, previous, seen_ids, run_id, resumed
                )
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

        persisted = await self._store.outcomes(identity, progress.generation)
        failed = [outcome for outcome in persisted.values() if outcome.status is ItemStatus.FAILED]
        report.failed = len(failed)
        progress.items_failed = len(failed)
        progress.last_error_code = normalize_code(ReasonCode.INGESTION_ERROR) if failed else None
        # A resumed suffix is still pending full reconciliation in run_once.
        if not resumed:
            progress.last_finished_at = datetime.now(UTC)
            if not failed:
                progress.last_success_at = progress.last_finished_at
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

    async def _sweep_deletions(
        self,
        identity: SourceIdentity,
        progress: SyncProgress,
        report: SyncReport,
        previous: MutableMapping[str, ItemOutcome],
        seen_ids: set[str],
        run_id: str,
        resumed: bool,
    ) -> None:
        """Tombstone items present in a prior run but absent from THIS listing.

        Two correctness rules (root review):

        - only a run that traversed the FULL listing from its start may
          delete. A RESUMED run starts mid-cursor, so earlier pages' items
          are legitimately absent from ``seen_ids`` — deleting them would be
          a false deletion. Resumed runs defer deletions to the next fresh
          run (bounded latency, never a wrong tombstone).
        - the sweep also covers a listing that is EMPTY (a source that
          emptied out): every mapped item is absent and must go.
        """
        if resumed:
            # Not this run's job: the traversal did not cover the listing.
            return
        deleted_ids = [eid for eid in list(previous.keys()) if eid not in seen_ids]
        for eid in deleted_ids:
            prior = previous.get(eid)
            if prior is None or prior.status is ItemStatus.DELETED:
                continue
            try:
                if self._deleter is not None:
                    await self._deleter(identity, eid, prior)
                outcome = ItemOutcome(
                    external_id=eid,
                    status=ItemStatus.DELETED,
                    checksum=prior.checksum,
                    size_bytes=prior.size_bytes,
                    run_id=run_id,
                    occurred_at=datetime.now(UTC),
                    generation=progress.generation,
                )
            except Exception:
                outcome = ItemOutcome(
                    external_id=eid,
                    status=ItemStatus.FAILED,
                    checksum=prior.checksum,
                    size_bytes=prior.size_bytes,
                    error_code=normalize_code(ReasonCode.INGESTION_ERROR),
                    error_message="connector operation failed",
                    run_id=run_id,
                    occurred_at=datetime.now(UTC),
                    generation=progress.generation,
                )
            await self._store.record_outcome(identity, outcome)
            previous[eid] = outcome
            if outcome.status is ItemStatus.DELETED:
                report.deleted += 1
                progress.items_deleted += 1
            else:
                report.failed += 1
                progress.items_failed += 1
                report.errors.append(f"{eid}: {outcome.error_code}")

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
        if (advertised is not None and prior is not None and prior.checksum == advertised
                and prior.status is not ItemStatus.FAILED):
            # Adapter already told us the bytes are identical (its own scan
            # checksum) and we recorded that exact checksum → unchanged, no
            # second download. Anything we cannot prove is fetched again.
            #
            # EXCEPTION (root review): an item whose prior outcome is DELETED
            # must REPUBLISH when it reappears — its document was tombstoned
            # and the mapping dropped, so "unchanged" would leave the source
            # without a live document forever.
            if prior.status is ItemStatus.DELETED:
                return await self._publish_item(
                    identity, generation, run_id, config, connector, item, advertised
                )
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
        except Exception:
            return ItemOutcome(
                external_id=item.external_id,
                status=ItemStatus.FAILED,
                checksum=None,
                size_bytes=item.size_bytes,
                # T-V1-025: persist a canonical registry code, not a raw string.
                error_code=normalize_code(ReasonCode.INGESTION_ERROR),
                error_message="connector operation failed",
                run_id=run_id,
                occurred_at=now,
                generation=generation,
            )
        checksum = hashlib.sha256(content).hexdigest()
        if prior is None or prior.status is ItemStatus.DELETED:
            # A reappeared item (prior DELETED) is CREATED again — its
            # document was tombstoned, so UNCHANGED would strand the source.
            status = ItemStatus.CREATED
        elif prior.checksum != checksum or prior.status is ItemStatus.FAILED:
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

    async def _publish_item(
        self,
        identity: SourceIdentity,
        generation: int,
        run_id: str,
        config: ConnectorConfig,
        connector: Connector,
        item: SourceItem,
        advertised_checksum: str,
    ) -> ItemOutcome:
        """Republish a reappeared (previously DELETED) item as CREATED.

        The advertised checksum is taken only when the fetched bytes match it
        (never on faith): a mismatch falls through to the normal fetch path
        so the persisted checksum always describes the published bytes.
        """
        now = datetime.now(UTC)
        try:
            content = await connector.fetch(config, item)
        except Exception:
            return ItemOutcome(
                external_id=item.external_id,
                status=ItemStatus.FAILED,
                checksum=None,
                size_bytes=item.size_bytes,
                error_code=normalize_code(ReasonCode.INGESTION_ERROR),
                error_message="connector operation failed",
                run_id=run_id,
                occurred_at=now,
                generation=generation,
            )
        checksum = hashlib.sha256(content).hexdigest()
        if checksum != advertised_checksum:
            # The adapter's claim did not hold — re-run the full comparison so
            # nothing is published on an unverified checksum.
            return await self._process_item(
                identity, generation, run_id, config, connector, item, {}
            )
        if self._publisher is not None:
            await self._publisher(identity, item, content, checksum)
        return ItemOutcome(
            external_id=item.external_id,
            status=ItemStatus.CREATED,
            checksum=checksum,
            size_bytes=len(content),
            run_id=run_id,
            occurred_at=now,
            generation=generation,
        )
