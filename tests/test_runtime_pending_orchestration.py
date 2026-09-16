"""SPEC-011 Phase 4 Pending scheduling and reevaluation evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from alert_correlation import AlertCorrelationPolicyEngine, DecisionType, EvaluationPhase
from alert_correlation.policy import DEFAULT_POLICY_REGISTRY, PolicyRegistry
from alert_correlation.state import (
    PendingGraceConfig,
    PendingStateService,
    ResolvedState,
    RetryDisposition,
    SqliteCorrelationStateStore,
    TerminalOutcome,
)
from incident_management import IncidentManager, SqliteIncidentStore
from runtime_orchestration import (
    DurableEventIntake,
    InitialCorrelationOrchestrator,
    PendingOrchestrator,
    PendingOutcomeKind,
    PendingSweepScheduler,
    RuntimeClock,
    RuntimeLifecycleState,
    load_runtime_config,
)
from shadow_management import ShadowManager, SqliteShadowStore


NOW = datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc)


def _event(
    event_id: str,
    event_type: str,
    detected_at: datetime,
    *,
    trace_id: str | None = None,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "detected_at": detected_at.isoformat().replace("+00:00", "Z"),
        "event_source": "metric_event_detection",
        "event_type": event_type,
        "detection_method": "rule_based",
        "severity": "HIGH",
        "confidence": 0.95,
        "service_name": "api",
        "trace_id": trace_id,
        "source_ip": None,
        "downstream_service": None,
        "external_service": None,
        "status": "OPEN",
        "triggered_features": {},
        "raw_log_sample": [],
    }


class _Events:
    def __init__(self, events):
        self.events = list(events)

    def read_all_authoritative(self):
        return self.events


class _Ready:
    state = RuntimeLifecycleState.READY


class _MutableTime:
    def __init__(self) -> None:
        self.wall = NOW
        self.monotonic = 0.0


class _PendingHarness:
    def __init__(self, path, events, registry=DEFAULT_POLICY_REGISTRY) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.events = _Events(events)
        self.time = _MutableTime()
        self.ready = _Ready()
        self.registry = registry
        self.state = SqliteCorrelationStateStore(path / "state.sqlite3")
        self.incidents = SqliteIncidentStore(str(path / "incident.sqlite3"))
        self.shadows = SqliteShadowStore(path / "shadow.sqlite3")
        incident_ids = count(1)
        self.incident_manager = IncidentManager(
            self.incidents, incident_id_factory=lambda: f"INC-{next(incident_ids)}"
        )
        self.shadow_manager = ShadowManager(
            self.shadows, registry, self.incidents
        )
        self.intake = DurableEventIntake(self.events)
        self.clock = RuntimeClock(
            lambda: self.time.wall,
            lambda: self.time.monotonic,
            lambda _seconds: pytest.fail("required tests must not sleep"),
        )
        self._compose(registry)

    def _compose(self, registry) -> None:
        engine = AlertCorrelationPolicyEngine(registry=registry)
        self.terminal = InitialCorrelationOrchestrator(
            event_intake=self.intake,
            state_store=self.state,
            policy_engine=engine,
            incident_store=self.incidents,
            incident_manager=self.incident_manager,
            shadow_store=self.shadows,
            shadow_manager=self.shadow_manager,
            clock=self.clock,
            readiness=self.ready,
        )
        self.pending_service = PendingStateService(
            self.state, registry.resolve_exact, PendingGraceConfig(30)
        )
        self.pending = PendingOrchestrator(
            event_intake=self.intake,
            state_store=self.state,
            pending_service=self.pending_service,
            policy_engine=engine,
            incident_store=self.incidents,
            terminal_executor=self.terminal,
            clock=self.clock,
            readiness=self.ready,
        )

    def recompose(self, registry) -> None:
        self.registry = registry
        self._compose(registry)

    def reopen_state(self) -> None:
        self.state.close()
        self.state = SqliteCorrelationStateStore(self.path / "state.sqlite3")
        self._compose(self.registry)

    def close(self) -> None:
        self.state.close()
        self.incidents.close()
        self.shadows.close()


@pytest.fixture
def pending_harnesses(tmp_path):
    opened = []

    def create(events, registry=DEFAULT_POLICY_REGISTRY):
        h = _PendingHarness(tmp_path / f"case-{len(opened)}", events, registry)
        opened.append(h)
        return h

    yield create
    for h in opened:
        h.close()


def _weak(event_id="EVT-WEAK", detected_at=NOW):
    return _event(event_id, "high_latency_detected", detected_at)


def _enter(h, event_id="EVT-WEAK"):
    result = h.pending.enter_initial_pending(event_id)
    assert result.outcome_kind is PendingOutcomeKind.ENTERED_PENDING
    return result.pending


def test_initial_enter_pending_uses_spec007_absolute_grace(pending_harnesses) -> None:
    h = pending_harnesses([_weak()])
    pending = _enter(h)

    assert pending.entered_pending_at == NOW
    assert pending.expires_at == NOW + timedelta(seconds=30)
    assert h.state.resolve("EVT-WEAK").pending == pending


def test_recheck_remains_pending_without_grace_reset(pending_harnesses) -> None:
    h = pending_harnesses([_weak()])
    original = _enter(h)
    h.time.wall = NOW + timedelta(seconds=12)

    outcome = h.pending.sweep_once().outcomes[0]

    assert outcome.outcome_kind is PendingOutcomeKind.REMAINS_PENDING
    assert outcome.evaluation_phase is EvaluationPhase.PENDING_RECHECK
    assert outcome.pending.entered_pending_at == original.entered_pending_at
    assert outcome.pending.expires_at == original.expires_at
    assert outcome.pending.policy_id == original.policy_id
    assert outcome.pending.policy_version == original.policy_version


@pytest.mark.parametrize(
    ("offset", "expected_phase"),
    [(29, EvaluationPhase.PENDING_RECHECK), (30, EvaluationPhase.PENDING_EXPIRED)],
)
def test_exact_expiry_boundary_is_decided_by_spec007(
    pending_harnesses, offset, expected_phase
) -> None:
    h = pending_harnesses([_weak()])
    _enter(h)
    h.time.wall = NOW + timedelta(seconds=offset)

    outcome = h.pending.sweep_once().outcomes[0]

    assert outcome.evaluation_phase is expected_phase
    if offset == 29:
        assert outcome.outcome_kind is PendingOutcomeKind.REMAINS_PENDING
    else:
        assert outcome.outcome_kind is PendingOutcomeKind.TERMINAL_PROCESSED
        assert outcome.terminal.processed.terminal_outcome is TerminalOutcome.CREATED_INCIDENT


def test_final_expiry_reevaluation_reuses_phase3_terminal_protocol(
    pending_harnesses
) -> None:
    h = pending_harnesses([_weak("EVT-EXPIRE")])
    original = _enter(h, "EVT-EXPIRE")
    h.time.wall = original.expires_at

    outcome = h.pending.sweep_once().outcomes[0]
    resolved = h.state.resolve("EVT-EXPIRE")

    assert outcome.outcome_kind is PendingOutcomeKind.TERMINAL_PROCESSED
    assert resolved.state is ResolvedState.TERMINAL_PROCESSED
    assert resolved.processed.terminal_outcome is TerminalOutcome.CREATED_INCIDENT
    assert resolved.pending is None
    assert h.incidents.event_has_incident_owner("EVT-EXPIRE") is True


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(10, PendingOutcomeKind.REMAINS_PENDING), (31, PendingOutcomeKind.TERMINAL_PROCESSED)],
)
def test_restart_does_not_reset_pending_entry_or_expiry(
    pending_harnesses, offset, expected
) -> None:
    h = pending_harnesses([_weak()])
    original = _enter(h)
    h.reopen_state()
    h.time.wall = NOW + timedelta(seconds=offset)

    outcome = h.pending.sweep_once().outcomes[0]

    assert outcome.outcome_kind is expected
    if outcome.pending is not None:
        assert outcome.pending.entered_pending_at == original.entered_pending_at
        assert outcome.pending.expires_at == original.expires_at


def test_historical_policy_missing_keeps_pending_and_creates_block(
    pending_harnesses
) -> None:
    h = pending_harnesses([_weak()])
    original = _enter(h)
    missing = PendingStateService(
        h.state, lambda _policy_id, _version: None, PendingGraceConfig(30)
    )
    h.pending._pending_service = missing
    h.time.wall = NOW + timedelta(seconds=5)

    outcome = h.pending.sweep_once().outcomes[0]
    resolved = h.state.resolve("EVT-WEAK")

    assert outcome.outcome_kind is PendingOutcomeKind.BLOCKED_POLICY_UNAVAILABLE
    assert resolved.state is ResolvedState.ACTIVE_PENDING_BLOCKED
    assert resolved.pending == original
    assert resolved.blocked.retry_disposition is RetryDisposition.REPAIR_REQUIRED
    assert resolved.blocked.policy_id == original.policy_id
    assert resolved.blocked.policy_version == original.policy_version

    original_block = resolved.blocked
    second = h.pending.sweep_once().outcomes[0]
    assert second.outcome_kind is PendingOutcomeKind.BLOCKED_PRESERVED
    assert h.state.resolve("EVT-WEAK").blocked == original_block


def test_exact_historical_policy_is_used_instead_of_latest(pending_harnesses) -> None:
    h = pending_harnesses([_weak()])
    original = _enter(h)
    v1 = replace(
        DEFAULT_POLICY_REGISTRY.resolve_exact(original.policy_id, original.policy_version),
        is_current=False,
    )
    v2 = replace(v1, policy_version="2.0", is_current=True)
    historical_registry = PolicyRegistry((v1, v2))
    h.recompose(historical_registry)
    h.time.wall = original.expires_at

    outcome = h.pending.sweep_once().outcomes[0]

    assert outcome.terminal.decision.policy_version == "1.0"
    assert outcome.terminal.processed.policy_version == "1.0"


def test_new_compatible_incident_uses_latest_views_not_candidate_snapshot(
    pending_harnesses
) -> None:
    weak = _weak(detected_at=NOW + timedelta(seconds=10))
    strong = _event(
        "EVT-STRONG", "cross_service_failure", NOW + timedelta(seconds=5), trace_id="TRACE-1"
    )
    h = pending_harnesses([weak, strong])
    original = _enter(h)
    assert h.incidents.list_correlation_views() == ()

    created = h.terminal.process_authoritative_event("EVT-STRONG")
    h.time.wall = NOW + timedelta(seconds=10)
    outcome = h.pending.sweep_once().outcomes[0]

    assert outcome.outcome_kind is PendingOutcomeKind.TERMINAL_PROCESSED
    assert outcome.evaluation_phase is EvaluationPhase.PENDING_RECHECK
    assert outcome.terminal.decision.decision_type is DecisionType.ATTACH_EXISTING
    assert outcome.terminal.processed.incident_id == created.processed.incident_id
    assert h.state.resolve("EVT-WEAK").pending is None
    assert original.expires_at == NOW + timedelta(seconds=30)


def test_fake_monotonic_scheduler_never_sleeps(pending_harnesses) -> None:
    h = pending_harnesses([_weak()])
    _enter(h)
    scheduler = PendingSweepScheduler(
        h.pending,
        cadence_seconds=load_runtime_config(
            "configs/runtime_orchestration.yaml"
        ).pending_scan_seconds,
        monotonic=lambda: h.time.monotonic,
    )

    assert scheduler.poll() is not None
    h.time.monotonic = 0.5
    assert scheduler.poll() is None
    h.time.monotonic = 1.0
    assert scheduler.poll() is not None
    assert scheduler.next_due_monotonic == 2.0


def test_multiple_pending_are_deterministic_and_fair(pending_harnesses) -> None:
    events = [_weak(f"EVT-{index}") for index in (3, 1, 2)]
    h = pending_harnesses(events)
    for event in events:
        _enter(h, event["event_id"])
    h.time.wall = NOW + timedelta(seconds=3)

    result = h.pending.sweep_once()

    assert result.candidate_event_ids == ("EVT-1", "EVT-2", "EVT-3")
    assert tuple(item.event_id for item in result.outcomes) == result.candidate_event_ids
    assert all(
        item.outcome_kind is PendingOutcomeKind.REMAINS_PENDING
        for item in result.outcomes
    )


def test_one_missing_pending_event_does_not_starve_later_candidates(
    pending_harnesses
) -> None:
    first = _weak("EVT-A")
    second = _weak("EVT-B")
    h = pending_harnesses([first, second])
    _enter(h, "EVT-A")
    _enter(h, "EVT-B")
    h.events.events = [second]
    h.time.wall = NOW + timedelta(seconds=4)

    result = h.pending.sweep_once()

    assert result.outcomes[0].outcome_kind is PendingOutcomeKind.EVENT_MISSING
    assert result.outcomes[1].event_id == "EVT-B"
    assert result.outcomes[1].outcome_kind is PendingOutcomeKind.REMAINS_PENDING
