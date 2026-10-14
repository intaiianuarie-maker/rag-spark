"""W6 schedule/trigger connector CONTRACTS — DISABLED by default (T-V4-024).

Shapes, validation, workspace scoping and credential-by-reference rules for
the two W6 entry surfaces, contracted now and INERT until explicitly enabled:

* **Schedule** (``cron`` / ``interval`` / ``one_shot``) — when a workflow
  should start.
* **Trigger** (``webhook`` / ``event`` / ``manual``) — how a workflow may be
  poked.

Flag-off discipline (same as the W3 adapter): a default-constructed connector
fires no timer, listens on no webhook and consumes no event — the firing
surfaces (:meth:`ScheduleConnector.tick`, :meth:`TriggerConnector.receive`)
REFUSE while disabled, and enabling requires BOTH a store and a runner
(enabling without either refuses loudly rather than degrading to a silent
no-op). While enabled, this module still only *accepts* and *evaluates*:
dispatching to the runner is the live ticket, so ``fired`` stands at the
number of runs actually started (0 here) and ``evaluated``/``consumed`` count
contract-level work.

Every spec is workspace-scoped (an unscoped spec is refused) and carries
credentials BY REFERENCE ONLY — the deny-list and the reference grammar are
dev2's ``credential_hygiene`` module (T-V4-020), imported rather than
re-implemented, so there is exactly one list in the tree.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .credential_hygiene import (
    CredentialReference,
    assert_references_only,
)

# --------------------------------------------------------------------------- #
# Bounds — stated with reasons
# --------------------------------------------------------------------------- #

#: Max characters in an id (schedule/trigger/workspace reference).  Ids enter
#: keys, logs and rejection echoes; 128 is generous and keeps echoes bounded.
MAX_ID_CHARS = 128

#: Max interval length (90 days).  A longer interval is a cron job in denial;
#: the schedule kinds exist so each has an honest, bounded representation.
MAX_SCHEDULE_INTERVAL_S = 7_776_000

#: Cron expressions: 5 whitespace-separated fields, bounded charset and total
#: length.  This is a SYNTAX SHAPE check — the cron evaluator itself is the
#: live ticket; the shape check is what keeps a hostile expression out now.
_CRON_FIELD = re.compile(r"^[0-9*/,\-A-Za-z]+$")
MAX_CRON_CHARS = 100
_CRON_FIELD_COUNT = 5

#: A webhook endpoint is a PATH on this surface, never a URL with a host:
#: accepting a host would make the connector an open redirect.
_WEBHOOK_PATH = re.compile(r"^/[A-Za-z0-9_][A-Za-z0-9_./-]{0,198}$")
MAX_ENDPOINT_CHARS = 200

#: Event names are dotted, lowercase (``document.ingested``).
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,5}$")
MAX_EVENT_NAME_CHARS = 128


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ConnectorError(RuntimeError):
    """Base class for connector refusals."""


class ConnectorDisabledError(ConnectorError):
    """A firing surface was used while the connector is disabled."""


class ConnectorNotArmedError(ConnectorError):
    """Enabling was attempted without the store/runner the connector needs."""


class ConnectorRejectedError(ValueError):
    """A spec was refused by the contract validation."""


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #


class ScheduleKind(StrEnum):
    CRON = "cron"
    INTERVAL = "interval"
    ONE_SHOT = "one_shot"


class TriggerKind(StrEnum):
    WEBHOOK = "webhook"
    EVENT = "event"
    MANUAL = "manual"


def _bound_id(value: str, field: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    if len(value) > MAX_ID_CHARS:
        raise ValueError(f"{field} exceeds {MAX_ID_CHARS} characters")
    if not re.match(r"^[A-Za-z0-9_.-]+$", value):
        raise ValueError(f"{field} must be an identifier (letters, digits, _ . -)")
    return value


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("run_at must be timezone-aware")
    return parsed


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


class ConnectorSpec(BaseModel):
    """Shared surface: every connector spec is workspace-scoped and disabled."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    enabled: bool = False
    #: Credential references (``vault:prod/api-key``); values are refused.
    credential_refs: list[CredentialReference] = Field(default_factory=list)
    #: Free-form connector config — credential VALUES are refused here too.
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workspace_id")
    @classmethod
    def _check_workspace(cls, value: str) -> str:
        return _bound_id(value, "workspace_id")

    @field_validator("config")
    @classmethod
    def _check_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        assert_references_only(value, where="connector config")
        return value

    @field_validator("credential_refs", mode="before")
    @classmethod
    def _parse_refs(cls, value: Any) -> Any:
        """Reference strings are parsed (a value-shaped string is refused)."""
        if isinstance(value, (list, tuple)):
            return [CredentialReference.parse(v) if isinstance(v, str) else v for v in value]
        return value


class ScheduleSpec(ConnectorSpec):
    """When a workflow should start.  Exactly the kind's own field is allowed."""

    schedule_id: str
    kind: ScheduleKind
    #: 5-field cron expression (kind=cron only).
    cron: str | None = None
    #: Seconds between runs (kind=interval only).
    interval_s: float | None = None
    #: ISO-8601 instant (kind=one_shot only).
    run_at: str | None = None

    @field_validator("schedule_id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _bound_id(value, "schedule_id")

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > MAX_CRON_CHARS:
            raise ValueError(f"cron expression exceeds {MAX_CRON_CHARS} characters")
        fields = value.split()
        if len(fields) != _CRON_FIELD_COUNT:
            raise ValueError(f"cron expression must have {_CRON_FIELD_COUNT} fields")
        for field in fields:
            if not _CRON_FIELD.match(field):
                raise ValueError(f"cron field {field!r} has disallowed characters")
        return value

    @field_validator("interval_s")
    @classmethod
    def _check_interval(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not 0 < value <= MAX_SCHEDULE_INTERVAL_S:
            raise ValueError(
                f"interval_s must be in (0, {MAX_SCHEDULE_INTERVAL_S}]"
            )
        return value

    @field_validator("run_at")
    @classmethod
    def _check_run_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _parse_timestamp(value)
        return value

    @model_validator(mode="after")
    def _check_kind_shape(self) -> ScheduleSpec:
        """Exactly the kind's own field may be present — no mixed shapes."""
        present = {
            field
            for field in ("cron", "interval_s", "run_at")
            if getattr(self, field) is not None
        }
        expected = {
            ScheduleKind.CRON: {"cron"},
            ScheduleKind.INTERVAL: {"interval_s"},
            ScheduleKind.ONE_SHOT: {"run_at"},
        }[self.kind]
        if present != expected:
            raise ConnectorRejectedError(
                f"schedule kind {self.kind.value} requires exactly {sorted(expected)}; "
                f"got {sorted(present)}"
            )
        return self


class TriggerSpec(ConnectorSpec):
    """How a workflow may be poked.  Exactly the kind's own field is allowed."""

    trigger_id: str
    kind: TriggerKind
    #: Webhook PATH on this surface (kind=webhook only; never a URL).
    endpoint: str | None = None
    #: Dotted event name (kind=event only).
    event_name: str | None = None

    @field_validator("trigger_id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _bound_id(value, "trigger_id")

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > MAX_ENDPOINT_CHARS:
            raise ValueError(f"endpoint exceeds {MAX_ENDPOINT_CHARS} characters")
        if "://" in value or "\\" in value:
            raise ValueError("endpoint must be a path on this surface, never a URL")
        if not _WEBHOOK_PATH.match(value):
            raise ValueError(
                "endpoint must be a path on this surface (/...), never a URL"
            )
        return value

    @field_validator("event_name")
    @classmethod
    def _check_event_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > MAX_EVENT_NAME_CHARS:
            raise ValueError(f"event_name exceeds {MAX_EVENT_NAME_CHARS} characters")
        if not _EVENT_NAME.match(value):
            raise ValueError(
                "event_name must be dotted lowercase (e.g. document.ingested)"
            )
        return value

    @model_validator(mode="after")
    def _check_kind_shape(self) -> TriggerSpec:
        present = {
            field
            for field in ("endpoint", "event_name")
            if getattr(self, field) is not None
        }
        expected = {
            TriggerKind.WEBHOOK: {"endpoint"},
            TriggerKind.EVENT: {"event_name"},
            TriggerKind.MANUAL: set(),
        }[self.kind]
        if present != expected:
            raise ConnectorRejectedError(
                f"trigger kind {self.kind.value} requires exactly {sorted(expected)}; "
                f"got {sorted(present)}"
            )
        return self


# --------------------------------------------------------------------------- #
# The store/runner an enabled connector needs
# --------------------------------------------------------------------------- #


class ConnectorStore(Protocol):
    """Durable home for registered specs (injected; no I/O in this module)."""

    async def put(self, spec: ConnectorSpec) -> None: ...


class ConnectorRunner(Protocol):
    """What an enabled connector would hand a firing to (injected)."""

    async def start(self, spec: ConnectorSpec) -> str: ...


class BaseConnector:
    """Shared flag-off discipline for both W6 surfaces."""

    def __init__(self, surface: str) -> None:
        self._surface = surface
        self._enabled = False
        self._store: ConnectorStore | None = None
        self._runner: ConnectorRunner | None = None
        #: workspace_id -> registered specs (per instance, never shared).
        self._specs: dict[str, list[ConnectorSpec]] = {}
        #: Contract-level counters (observability; asserted in tests).
        self.registered = 0
        self.evaluated = 0
        #: Runs actually started: 0 in this ticket by construction — the
        #: connector accepts and evaluates; dispatching is the live ticket.
        self.fired = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def register(self, spec: ConnectorSpec) -> None:
        """Validate + hold a spec (inert: registering never arms anything)."""
        self._specs.setdefault(spec.workspace_id, []).append(spec)
        self.registered += 1

    def for_workspace(self, workspace_id: str) -> list[ConnectorSpec]:
        """Specs for one workspace (scoped: other workspaces are invisible)."""
        return list(self._specs.get(workspace_id, ()))

    @property
    def workspaces(self) -> list[str]:
        return sorted(self._specs)

    def enable(self, *, store: ConnectorStore | None, runner: ConnectorRunner | None) -> None:
        """Enable the connector — refusing loudly without BOTH dependencies.

        Enabling with one of the two missing is a configuration error, not a
        degraded mode: a schedule connector without a store cannot survive a
        restart and one without a runner cannot start a run, and either gap
        silently no-oping is exactly the false-ready shape this package
        refuses.
        """
        if store is None or runner is None:
            raise ConnectorNotArmedError(
                f"{self._surface} connector requires both a store and a runner "
                f"to enable (store={type(store).__name__ if store else None}, "
                f"runner={type(runner).__name__ if runner else None}); refusing "
                "to enable half-armed"
            )
        self._store = store
        self._runner = runner
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def _require_enabled(self, action: str) -> None:
        if not self._enabled:
            raise ConnectorDisabledError(
                f"{self._surface} connector is disabled (default OFF): {action} "
                "refused; enable it with a store and a runner first"
            )

class ScheduleConnector(BaseConnector):
    """Contract surface for schedules (T-V4-024).

    ``due`` is pure evaluation (it fires nothing and works disabled — it is
    arithmetic, not a timer); ``tick`` is the firing surface and refuses while
    disabled.  Cron evaluation is the live ticket; an interval/one-shot due
    check is bounded arithmetic and is evaluated here.
    """

    def __init__(self) -> None:
        super().__init__("schedule")
        #: schedule_id -> registration epoch (the interval counts from here).
        self._interval_anchor: dict[str, float] = {}

    def register(self, spec: ConnectorSpec) -> None:
        super().register(spec)
        if isinstance(spec, ScheduleSpec) and spec.kind is ScheduleKind.INTERVAL:
            self._interval_anchor.setdefault(spec.schedule_id, datetime.now(UTC).timestamp())

    def schedules_for(self, workspace_id: str) -> list[ScheduleSpec]:
        """Schedule specs for one workspace (typed view of the registry)."""
        return [
            spec
            for spec in self.for_workspace(workspace_id)
            if isinstance(spec, ScheduleSpec)
        ]

    def due(
        self,
        now: datetime,
        *,
        workspace_id: str | None = None,
    ) -> list[ScheduleSpec]:
        """Specs due at *now* (workspace-scoped when asked).

        interval/one-shot are evaluated (bounded arithmetic); a cron spec is
        never reported due by this contract — cron evaluation is the live
        ticket, and a shape-valid cron expression is not a cron evaluator.
        """
        self.evaluated += 1
        scopes = [workspace_id] if workspace_id is not None else list(self._specs)
        due: list[ScheduleSpec] = []
        for scope in scopes:
            for spec in self._specs.get(scope, ()):
                if not isinstance(spec, ScheduleSpec):
                    continue
                if spec.kind is ScheduleKind.INTERVAL:
                    anchor = self._interval_anchor[spec.schedule_id]
                    if now.timestamp() - anchor >= float(spec.interval_s or 0):
                        due.append(spec)
                elif spec.kind is ScheduleKind.ONE_SHOT:
                    if now >= _parse_timestamp(spec.run_at or ""):
                        due.append(spec)
        return due

    def tick(self, now: datetime) -> list[str]:
        """The firing surface: refuses while disabled; when enabled it
        evaluates and reports due ids — it starts no run (``fired`` stays 0
        until the live ticket wires dispatch)."""
        self._require_enabled("tick")
        return [spec.schedule_id for spec in self.due(now)]


class TriggerConnector(BaseConnector):
    """Contract surface for triggers (T-V4-024).

    ``receive`` is the listening surface: it refuses while disabled, and when
    enabled it ACCEPTS a delivery against a registered, workspace-matching
    trigger and records it (``consumed``); dispatching to the runner is the
    live ticket, so no event is acted on here.
    """

    def __init__(self) -> None:
        super().__init__("trigger")
        self._by_endpoint: dict[str, TriggerSpec] = {}
        self._by_event: dict[str, TriggerSpec] = {}
        self.consumed = 0
        self._sequence = 0

    def register(self, spec: ConnectorSpec) -> None:
        if not isinstance(spec, TriggerSpec):
            raise ConnectorRejectedError(
                "trigger connector registers TriggerSpec only"
            )
        super().register(spec)
        if spec.kind is TriggerKind.WEBHOOK and spec.endpoint:
            self._by_endpoint[spec.endpoint] = spec
        if spec.kind is TriggerKind.EVENT and spec.event_name:
            self._by_event[spec.event_name] = spec

    def resolve(self, address: str, *, workspace_id: str) -> TriggerSpec | None:
        """Resolve a webhook path / event name WITHIN one workspace."""
        for spec in (
            *self._by_endpoint.values(),
            *self._by_event.values(),
        ):
            if spec.workspace_id != workspace_id:
                continue  # cross-workspace delivery is never resolved
            if spec.endpoint == address or spec.event_name == address:
                return spec
        return None

    def receive(
        self,
        address: str,
        payload: bytes,
        *,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Accept one delivery (the listening surface).

        Refuses while disabled.  When enabled: an unknown or cross-workspace
        address is refused (404-shaped, never accepted blindly), a manual
        trigger has no address (``start``), and the receipt records what was
        accepted — the payload is NOT dispatched here.
        """
        self._require_enabled("receive")
        spec = self.resolve(address, workspace_id=workspace_id)
        if spec is None and address != "start":
            raise ConnectorRejectedError(
                f"no trigger registered for {address!r} in workspace "
                f"{workspace_id!r}"
            )
        self._sequence += 1
        self.consumed += 1
        return {
            "trigger_id": spec.trigger_id if spec else address,
            "workspace_id": workspace_id,
            "sequence": self._sequence,
            "accepted_bytes": len(payload),
            "accepted_at": datetime.now(UTC).isoformat(),
        }
