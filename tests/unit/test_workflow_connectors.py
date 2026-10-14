"""T-V4-024 — W6 schedule/trigger connector contracts, DISABLED by default.

Contract surface (stated in ``workflow_engine.connectors``):

* ``ScheduleSpec`` — ``cron`` / ``interval`` / ``one_shot``; exactly the
  kind's own field may be present (mixed shapes refused); bounded cron shape,
  bounded interval (0, 90 days], parseable timezone-aware ``run_at``.
* ``TriggerSpec`` — ``webhook`` / ``event`` / ``manual``; a webhook endpoint
  is a PATH on this surface (a URL/host is refused — no open redirect), an
  event name is dotted lowercase.
* Both: workspace-scoped (unscoped refused), credentials BY REFERENCE ONLY
  (dev2's ``credential_hygiene`` deny-list — one list in the tree).
* Both connectors: DISABLED by default — the firing surfaces (``tick`` /
  ``receive``) refuse while disabled, and enabling requires BOTH a store and
  a runner (half-armed refuses loudly).  Even enabled, this contract accepts
  and evaluates only: ``fired`` stands at 0.

Both metrics: enabled-by-default surfaces: 0; unscoped or
credential-by-value specs accepted: 0.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from workflow_engine.connectors import (
    ConnectorDisabledError,
    ConnectorNotArmedError,
    ConnectorRejectedError,
    ScheduleConnector,
    ScheduleKind,
    ScheduleSpec,
    TriggerConnector,
    TriggerKind,
    TriggerSpec,
)
from workflow_engine.credential_hygiene import (
    CredentialReferenceError,
    CredentialValueError,
)


class _Store:
    """Durable home stub (injected at enable time)."""

    def __init__(self) -> None:
        self.saved: list[Any] = []

    async def put(self, spec: Any) -> None:
        self.saved.append(spec)


class _Runner:
    """Runner stub — records what it was asked to start (nothing, here)."""

    def __init__(self) -> None:
        self.started: list[Any] = []

    async def start(self, spec: Any) -> str:
        self.started.append(spec)
        return "run-stub"


def _schedule(**over: Any) -> ScheduleSpec:
    base: dict[str, Any] = {
        "workspace_id": "ws-1",
        "schedule_id": "sched-1",
        "kind": ScheduleKind.INTERVAL,
        "interval_s": 60,
    }
    base.update(over)
    return ScheduleSpec(**base)


def _trigger(**over: Any) -> TriggerSpec:
    base: dict[str, Any] = {
        "workspace_id": "ws-1",
        "trigger_id": "hook-1",
        "kind": TriggerKind.WEBHOOK,
        "endpoint": "/hooks/ingest",
    }
    base.update(over)
    return TriggerSpec(**base)


# ---------------------------------------------------------------------------
# Metric 1 — enabled-by-default surfaces: 0
# ---------------------------------------------------------------------------


class TestDisabledByDefault:
    def test_default_schedule_connector_fires_nothing(self) -> None:
        connector = ScheduleConnector()
        assert connector.enabled is False
        assert connector.registered == 0
        assert connector.fired == 0
        connector.register(_schedule())
        # Registering is inert: no timer, no firing.
        assert connector.registered == 1
        assert connector.fired == 0
        with pytest.raises(ConnectorDisabledError):
            connector.tick(datetime.now(UTC))

    def test_default_trigger_connector_listens_nothing(self) -> None:
        connector = TriggerConnector()
        assert connector.enabled is False
        connector.register(_trigger())
        with pytest.raises(ConnectorDisabledError):
            connector.receive("/hooks/ingest", b"{}", workspace_id="ws-1")
        assert connector.consumed == 0

    def test_enabling_without_store_or_runner_refuses(self) -> None:
        connector = ScheduleConnector()
        with pytest.raises(ConnectorNotArmedError):
            connector.enable(store=None, runner=None)
        with pytest.raises(ConnectorNotArmedError):
            connector.enable(store=_Store(), runner=None)
        with pytest.raises(ConnectorNotArmedError):
            connector.enable(store=None, runner=_Runner())
        assert connector.enabled is False

    def test_enabled_connector_still_starts_nothing(self) -> None:
        """This ticket is contract-only: even armed, the connector accepts and
        evaluates; dispatching to the runner is the live ticket, so ``fired``
        stands at 0 while the surfaces work."""
        connector = ScheduleConnector()
        connector.register(_schedule(interval_s=0.001))
        connector.enable(store=_Store(), runner=_Runner())
        assert connector.enabled is True
        now = datetime.now(UTC)
        due_ids = connector.tick(now + timedelta(seconds=1))
        assert due_ids == ["sched-1"]  # evaluation works
        assert connector.fired == 0  # nothing started
        connector.disable()
        with pytest.raises(ConnectorDisabledError):
            connector.tick(datetime.now(UTC))

    def test_enabled_trigger_accepts_without_dispatching(self) -> None:
        connector = TriggerConnector()
        connector.register(_trigger())
        runner = _Runner()
        connector.enable(store=_Store(), runner=runner)
        receipt = connector.receive(
            "/hooks/ingest", b'{"a":1}', workspace_id="ws-1"
        )
        assert receipt["trigger_id"] == "hook-1"
        assert receipt["sequence"] == 1
        assert connector.consumed == 1
        assert runner.started == []  # no dispatch in this ticket
        assert connector.fired == 0


# ---------------------------------------------------------------------------
# Metric 2 — unscoped or credential-by-value specs accepted: 0
# ---------------------------------------------------------------------------


class TestHostileBattery:
    @pytest.mark.parametrize(
        "over",
        [
            {"workspace_id": ""},
            {"workspace_id": "   "},
            {"workspace_id": "x" * 129},
            {"workspace_id": "ws 1"},
            {"workspace_id": "ws/../other"},
            {"schedule_id": ""},
            {"schedule_id": "x" * 129},
            {"schedule_id": "id with spaces"},
            # kind-shape refusals: exactly the kind's own field is allowed.
            {"kind": ScheduleKind.CRON},
            {"kind": ScheduleKind.CRON, "cron": "0 3 * * *", "interval_s": 60},
            {"kind": ScheduleKind.INTERVAL, "interval_s": None},
            {"kind": ScheduleKind.INTERVAL, "interval_s": 0},
            {"kind": ScheduleKind.INTERVAL, "interval_s": -5},
            {"kind": ScheduleKind.INTERVAL, "interval_s": 7_776_001},
            {"kind": ScheduleKind.ONE_SHOT, "run_at": None},
            {"kind": ScheduleKind.ONE_SHOT, "run_at": "not-a-timestamp"},
            {"kind": ScheduleKind.ONE_SHOT, "run_at": "2026-01-01T00:00:00"},
            {"kind": ScheduleKind.CRON, "cron": "0 3 * *"},
            {"kind": ScheduleKind.CRON, "cron": "0 3 * * * *"},
            {"kind": ScheduleKind.CRON, "cron": "0 3 * * $(rm -rf /)"},
            {"kind": ScheduleKind.CRON, "cron": "0 3 * * " + "1" * 200},
        ],
    )
    def test_hostile_schedules_refused(self, over: dict[str, Any]) -> None:
        with pytest.raises((ValueError, ConnectorRejectedError)):
            _schedule(**over)

    @pytest.mark.parametrize(
        "over",
        [
            {"workspace_id": ""},
            {"kind": TriggerKind.WEBHOOK, "endpoint": None},
            {"kind": TriggerKind.WEBHOOK, "endpoint": "hooks/ingest"},
            {"kind": TriggerKind.WEBHOOK, "endpoint": "https://evil.example/hooks"},
            {"kind": TriggerKind.WEBHOOK, "endpoint": "//evil.example/hooks"},
            {"kind": TriggerKind.WEBHOOK, "endpoint": "/hooks/" + "x" * 300},
            {"kind": TriggerKind.EVENT, "event_name": None},
            {"kind": TriggerKind.EVENT, "event_name": "Document.Ingested"},
            {"kind": TriggerKind.EVENT, "event_name": "nested"},
            {"kind": TriggerKind.EVENT, "event_name": "a.b.c.d.e.f.g"},
            {"kind": TriggerKind.EVENT, "endpoint": "/hooks/x"},
            {"kind": TriggerKind.MANUAL, "endpoint": "/hooks/x"},
            {"trigger_id": ""},
            {"trigger_id": "x" * 129},
        ],
    )
    def test_hostile_triggers_refused(self, over: dict[str, Any]) -> None:
        with pytest.raises((ValueError, ConnectorRejectedError)):
            _trigger(**over)

    @pytest.mark.parametrize(
        "config",
        [
            {"api_key": "sk-live-abcdefghijklmnopqrstuvwxyz012345"},
            {"password": "hunter2"},
            {"authorization": "Bearer abcdefghijklmnopqrstuvwxyz012345"},
            {"nested": {"secret": "sk-live-abcdefghijklmnopqrstuvwxyz012345"}},
            # A value wearing a reference's name is still a value.
            {"credential_ref": "sk-live-abcdefghijklmnopqrstuvwxyz012345"},
            {"api_key_ref": "sk-live-abcdefghijklmnopqrstuvwxyz012345"},
        ],
    )
    def test_credential_values_refused(self, config: dict[str, Any]) -> None:
        with pytest.raises((CredentialValueError, CredentialReferenceError)):
            _schedule(config=config)
        with pytest.raises((CredentialValueError, CredentialReferenceError)):
            _trigger(config=config)

    def test_credential_reference_forms_pass(self) -> None:
        spec = _schedule(
            config={"credential_ref": "vault:prod/api-key"},
            credential_refs=["vault:prod/api-key", "db-credential"],
        )
        assert spec.credential_refs
        trig = _trigger(config={"credential_ref": "vault:prod/webhook-token"})
        assert trig.config["credential_ref"] == "vault:prod/webhook-token"

    def test_valid_shapes_pass(self) -> None:
        # cron
        cron = _schedule(
            kind=ScheduleKind.CRON, cron="0 3 * * *", interval_s=None
        )
        assert cron.cron == "0 3 * * *"
        # one-shot
        one_shot = _schedule(
            kind=ScheduleKind.ONE_SHOT,
            interval_s=None,
            run_at="2026-01-01T00:00:00+00:00",
        )
        assert one_shot.run_at is not None
        assert one_shot.run_at.endswith("+00:00")
        # event trigger
        event = _trigger(
            kind=TriggerKind.EVENT, event_name="document.ingested", endpoint=None
        )
        assert event.event_name == "document.ingested"
        # manual trigger
        manual = _trigger(kind=TriggerKind.MANUAL, endpoint=None)
        assert manual.kind is TriggerKind.MANUAL


# ---------------------------------------------------------------------------
# Workspace scoping + due evaluation
# ---------------------------------------------------------------------------


class TestScopingAndEvaluation:
    def test_workspace_scoping(self) -> None:
        connector = ScheduleConnector()
        connector.register(_schedule(schedule_id="a"))
        # A one-shot already in the past IS due — so `due` scoping is testable.
        past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        connector.register(
            _schedule(
                workspace_id="ws-2",
                schedule_id="b",
                kind=ScheduleKind.ONE_SHOT,
                interval_s=None,
                run_at=past,
            )
        )
        assert [s.schedule_id for s in connector.schedules_for("ws-1")] == ["a"]
        assert [s.schedule_id for s in connector.schedules_for("ws-2")] == ["b"]
        assert connector.for_workspace("ws-3") == []
        assert connector.workspaces == ["ws-1", "ws-2"]
        now = datetime.now(UTC)
        # Scoped evaluation sees only the asked workspace.
        assert [s.schedule_id for s in connector.due(now, workspace_id="ws-2")] == ["b"]
        assert connector.due(now, workspace_id="ws-3") == []
        assert [s.schedule_id for s in connector.due(now)] == ["b"]

    def test_cross_workspace_trigger_delivery_refused(self) -> None:
        connector = TriggerConnector()
        connector.register(_trigger(trigger_id="hook-1"))
        connector.enable(store=_Store(), runner=_Runner())
        # Registered in ws-1; a ws-2 delivery does not resolve.
        with pytest.raises(ConnectorRejectedError):
            connector.receive("/hooks/ingest", b"{}", workspace_id="ws-2")

    def test_interval_due_boundary(self) -> None:
        connector = ScheduleConnector()
        connector.register(_schedule(interval_s=60))
        anchor = datetime.now(UTC)  # the interval counts from registration
        assert connector.due(anchor + timedelta(seconds=59)) == []
        assert [s.schedule_id for s in connector.due(anchor + timedelta(seconds=60))] == ["sched-1"]

    def test_one_shot_due_boundary(self) -> None:
        connector = ScheduleConnector()
        run_at = datetime.now(UTC) + timedelta(seconds=30)
        connector.register(
            _schedule(
                kind=ScheduleKind.ONE_SHOT,
                interval_s=None,
                run_at=run_at.isoformat(),
            )
        )
        assert connector.due(run_at - timedelta(seconds=1)) == []
        assert len(connector.due(run_at)) == 1

    def test_cron_is_shape_validated_but_not_evaluated(self) -> None:
        """A shape-valid cron expression is not a cron evaluator: this contract
        refuses nothing valid, but reports no cron due (live ticket)."""
        connector = ScheduleConnector()
        connector.register(_schedule(kind=ScheduleKind.CRON, cron="0 3 * * *", interval_s=None))
        now = datetime.now(UTC)
        assert connector.due(now + timedelta(days=365)) == []
