"""SPEC-011 Phase 7 host E2E evidence over the production Runtime core."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

import pytest

from alert_correlation import AlertCorrelationPolicyEngine, DEFAULT_POLICY_REGISTRY
from alert_correlation.state import (
    PendingStateService,
    SqliteCorrelationStateStore,
    TerminalOutcome,
)
from event_detection.store.event_store import EventStore
from incident_management import (
    AssignmentPolicyConfig,
    IncidentDomainError,
    IncidentErrorCode,
    IncidentManager,
    IncidentStatus,
    SqliteIncidentStore,
)
from runtime_orchestration import (
    AuthoritativeTerminalRetryExecutor,
    AutoAssignOrchestrator,
    CorrelationRuntimeCore,
    DurableEventIntake,
    DurableRetryController,
    InitialCorrelationOrchestrator,
    PendingOrchestrator,
    PendingSweepScheduler,
    RuntimeBootstrap,
    RuntimeClock,
    RuntimeLifecycleState,
    RuntimeNotReadyError,
    RuntimeStateRecoveryClassifier,
    RuntimeStopController,
    RuntimeWorker,
    RuntimeWorkerExit,
    RuntimeWorkerMode,
    RuntimeWorkKind,
    RuntimeWorkStatus,
    SqliteRuntimeWorkStore,
    StdlibRuntimeTelemetry,
    TerminalFailureRecorder,
    automatic_assignment_operation_id,
)
from shadow_management import ShadowManager, SqliteShadowStore


NOW = datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc)
ASSIGNMENT_POLICY = AssignmentPolicyConfig(
    "POC-ROUND-ROBIN", "1.0", ("engineer-a", "engineer-b"), "supervisor"
)
AUTOMATION_ACTOR = "runtime-auto-assign"


def _event(
    event_id: str,
    event_type: str,
    detected_at: datetime,
    *,
    source_ip: str | None = None,
    trace_id: str | None = None,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "detected_at": detected_at.isoformat().replace("+00:00", "Z"),
        "event_source": "log_event_detection",
        "event_type": event_type,
        "detection_method": "rule_based",
        "severity": "HIGH",
        "confidence": 0.99,
        "service_name": "auth-api",
        "trace_id": trace_id,
        "source_ip": source_ip,
        "downstream_service": None,
        "external_service": None,
        "status": "OPEN",
        "triggered_features": {},
        "raw_log_sample": [],
    }


class _Time:
    def __init__(self) -> None:
        self.wall = NOW
        self.monotonic = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> RuntimeClock:
        return RuntimeClock(
            lambda: self.wall,
            lambda: self.monotonic,
            lambda seconds: self.sleeps.append(seconds),
        )


class _HostRuntime:
    def __init__(self, root, events, *, time: _Time | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.time = time or _Time()
        self.event_store = EventStore(root / "events.jsonl")
        if not self.event_store.path.exists():
            for event in events:
                self.event_store.write(event)
        self.state = SqliteCorrelationStateStore(root / "state.sqlite3")
        self.incidents = SqliteIncidentStore(str(root / "incidents.sqlite3"))
        self.shadows = SqliteShadowStore(root / "shadows.sqlite3")
        self.work = SqliteRuntimeWorkStore(root / "runtime.sqlite3")
        self.clock = self.time.clock()
        self.telemetry = StdlibRuntimeTelemetry(
            logging.getLogger(f"runtime.host-e2e.{root.name}")
        )
        self.intake = DurableEventIntake(self.event_store)
        self.bootstrap = RuntimeBootstrap(
            config_path="configs/runtime_orchestration.yaml",
            event_intake=self.intake,
            state_recovery=RuntimeStateRecoveryClassifier(self.state),
            runtime_work_store=self.work,
            clock=self.clock,
        )
        self.incident_manager = IncidentManager(self.incidents)
        self.shadow_manager = ShadowManager(
            self.shadows, DEFAULT_POLICY_REGISTRY, self.incidents
        )
        self.retry = DurableRetryController(
            work_store=self.work,
            retry_delays_seconds=(1.0, 2.0, 4.0, 8.0),
            clock=self.clock,
            telemetry=self.telemetry,
        )
        recorder = TerminalFailureRecorder(
            work_store=self.work, retry=self.retry, clock=self.clock
        )
        self.assignment = AutoAssignOrchestrator(
            state_store=self.state,
            incident_store=self.incidents,
            incident_manager=self.incident_manager,
            work_store=self.work,
            assignment_policy=ASSIGNMENT_POLICY,
            automation_actor=AUTOMATION_ACTOR,
            retry_delays_seconds=(1.0, 2.0, 4.0, 8.0),
            clock=self.clock,
            telemetry=self.telemetry,
        )
        self.terminal = InitialCorrelationOrchestrator(
            event_intake=self.intake,
            state_store=self.state,
            policy_engine=AlertCorrelationPolicyEngine(),
            incident_store=self.incidents,
            incident_manager=self.incident_manager,
            shadow_store=self.shadows,
            shadow_manager=self.shadow_manager,
            clock=self.clock,
            readiness=self.bootstrap,
            post_create_obligation=self.assignment,
            terminal_failure_recorder=recorder,
            telemetry=self.telemetry,
        )
        self.pending = PendingOrchestrator(
            event_intake=self.intake,
            state_store=self.state,
            pending_service=PendingStateService(
                self.state, DEFAULT_POLICY_REGISTRY.resolve_exact
            ),
            policy_engine=AlertCorrelationPolicyEngine(),
            incident_store=self.incidents,
            terminal_executor=self.terminal,
            clock=self.clock,
            readiness=self.bootstrap,
            telemetry=self.telemetry,
        )
        stop = RuntimeStopController()
        self.core = CorrelationRuntimeCore(
            bootstrap=self.bootstrap,
            initial_dispatcher=self.pending,
            pending_scheduler=PendingSweepScheduler(
                self.pending, cadence_seconds=1.0, monotonic=self.clock.monotonic
            ),
            auto_assign=self.assignment,
            retry=self.retry,
            retry_executor=AuthoritativeTerminalRetryExecutor(
                state_store=self.state,
                terminal_executor=self.terminal,
                work_store=self.work,
                clock=self.clock,
            ),
            clock=self.clock,
            stop=stop,
            telemetry=self.telemetry,
        )
        self.worker = RuntimeWorker(
            self.core,
            clock=self.clock,
            idle_poll_seconds=1.0,
            stop=stop,
            telemetry=self.telemetry,
        )

    def run_until_idle(self) -> None:
        assert (
            self.worker.run(RuntimeWorkerMode.RUN_UNTIL_IDLE)
            is RuntimeWorkerExit.IDLE
        )

    def close(self) -> None:
        self.state.close()
        self.incidents.close()
        self.shadows.close()
        self.work.close()


def test_host_four_path_e2e_with_pending_expiry_and_telemetry(tmp_path, caplog) -> None:
    events = [
        _event("EVT-A-CREATE", "brute_force_detected", NOW, source_ip="203.0.113.7"),
        _event(
            "EVT-B-ATTACH",
            "brute_force_detected",
            NOW + timedelta(seconds=1),
            source_ip="203.0.113.7",
        ),
        _event("EVT-PENDING", "high_latency_detected", NOW + timedelta(seconds=2)),
        _event("EVT-SHADOW", "general_log_anomaly", NOW + timedelta(seconds=3)),
    ]
    runtime = _HostRuntime(tmp_path / "host-four-path", events)
    try:
        with caplog.at_level(logging.INFO, logger=runtime.telemetry._logger.name):
            runtime.run_until_idle()

        created = runtime.state.resolve("EVT-A-CREATE").processed
        attached = runtime.state.resolve("EVT-B-ATTACH").processed
        pending = runtime.state.resolve("EVT-PENDING").pending
        shadowed = runtime.state.resolve("EVT-SHADOW").processed
        assert created.terminal_outcome is TerminalOutcome.CREATED_INCIDENT
        assert attached.terminal_outcome is TerminalOutcome.ATTACHED_TO_INCIDENT
        assert attached.incident_id == created.incident_id
        assert pending is not None
        assert shadowed.terminal_outcome is TerminalOutcome.SHADOWED
        assert runtime.shadows.get_shadow_by_event_id("EVT-SHADOW") is not None

        incident = runtime.incidents.get_incident(created.incident_id)
        assert incident.status is IncidentStatus.ASSIGNED
        assert incident.event_ids == ("EVT-A-CREATE", "EVT-B-ATTACH")
        workflow_id = automatic_assignment_operation_id(
            created.incident_id, AUTOMATION_ACTOR
        )
        assert runtime.incidents.get_workflow_operation_result(workflow_id) is not None
        assert len(runtime.incidents.list_workflow_audit(created.incident_id)) == 1

        runtime.time.wall = pending.expires_at
        runtime.time.monotonic += 1.0
        runtime.run_until_idle()
        expired = runtime.state.resolve("EVT-PENDING")
        assert expired.pending is None
        assert expired.processed.terminal_outcome is TerminalOutcome.CREATED_INCIDENT
        assert runtime.incidents.get_incident(expired.processed.incident_id).status is IncidentStatus.ASSIGNED

        messages = "\n".join(record.message for record in caplog.records)
        assert '"runtime_event":"STARTUP"' in messages
        assert '"runtime_event":"READY"' in messages
        assert '"runtime_event":"WORK_COMPLETED"' in messages
        assert '"runtime_event":"RECONCILIATION"' in messages
        assert '"stage":"AUTHORITATIVE_EVENT_INTAKE"' in messages
        assert '"claim_result":"ACQUIRED"' in messages
        assert '"stage":"DOMAIN_ATTEMPT"' in messages
        assert '"source_domain":"SPEC-009"' in messages
    finally:
        runtime.close()


def test_startup_recovers_unresolved_intent_without_policy_reevaluation(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "startup-intent"
    event = _event("EVT-RECOVER-INTENT", "brute_force_detected", NOW, source_ip="203.0.113.80")
    first = _HostRuntime(root, [event])
    first.core.startup()

    def lose_before_domain(_request):
        raise RuntimeError("crash after Intent")

    monkeypatch.setattr(first.incident_manager, "apply_correlation_mutation", lose_before_domain)
    with pytest.raises(RuntimeError, match="after Intent"):
        first.terminal.process_authoritative_event("EVT-RECOVER-INTENT")
    intent = first.state.resolve("EVT-RECOVER-INTENT").intent
    assert intent is not None
    first.close()

    restarted = _HostRuntime(root, [event])
    monkeypatch.setattr(
        AlertCorrelationPolicyEngine,
        "evaluate",
        lambda *_args: (_ for _ in ()).throw(AssertionError("policy reevaluated")),
    )
    try:
        restarted.core.startup()
        resolved = restarted.state.resolve("EVT-RECOVER-INTENT")
        assert restarted.bootstrap.state is RuntimeLifecycleState.READY
        assert resolved.processed is not None
        assert restarted.incidents.get_operation_result(intent.operation_id) is not None
    finally:
        restarted.close()


def test_startup_reconciles_domain_receipt_before_processed_without_second_call(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "startup-receipt"
    event = _event("EVT-RECOVER-RECEIPT", "brute_force_detected", NOW, source_ip="203.0.113.81")
    first = _HostRuntime(root, [event])
    first.core.startup()
    original = first.incident_manager.apply_correlation_mutation

    def commit_then_lose(request):
        original(request)
        raise RuntimeError("lost response")

    monkeypatch.setattr(first.incident_manager, "apply_correlation_mutation", commit_then_lose)
    with pytest.raises(RuntimeError, match="lost response"):
        first.terminal.process_authoritative_event("EVT-RECOVER-RECEIPT")
    operation_id = first.state.resolve("EVT-RECOVER-RECEIPT").intent.operation_id
    assert first.incidents.get_operation_result(operation_id) is not None
    first.close()

    restarted = _HostRuntime(root, [event])
    monkeypatch.setattr(
        restarted.incident_manager,
        "apply_correlation_mutation",
        lambda _request: (_ for _ in ()).throw(AssertionError("second domain mutation")),
    )
    try:
        restarted.core.startup()
        assert restarted.state.resolve("EVT-RECOVER-RECEIPT").processed is not None
        assert restarted.incidents.get_operation_result(operation_id) is not None
    finally:
        restarted.close()


def test_repeated_startup_interruption_rediscovers_same_operation(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repeated-startup-interruption"
    event = _event(
        "EVT-REPEATED-STARTUP",
        "brute_force_detected",
        NOW,
        source_ip="203.0.113.86",
    )
    first = _HostRuntime(root, [event])
    first.core.startup()
    monkeypatch.setattr(
        first.incident_manager,
        "apply_correlation_mutation",
        lambda _request: (_ for _ in ()).throw(RuntimeError("crash after Intent")),
    )
    with pytest.raises(RuntimeError, match="after Intent"):
        first.terminal.process_authoritative_event("EVT-REPEATED-STARTUP")
    operation_id = first.state.resolve("EVT-REPEATED-STARTUP").intent.operation_id
    first.close()

    restarted = _HostRuntime(root, [event])
    original = restarted.incident_manager.apply_correlation_mutation

    def commit_then_interrupt(request):
        original(request)
        raise RuntimeError("startup interrupted after commit")

    monkeypatch.setattr(
        restarted.incident_manager,
        "apply_correlation_mutation",
        commit_then_interrupt,
    )
    try:
        with pytest.raises(RuntimeError, match="startup interrupted"):
            restarted.core.startup()
        assert restarted.bootstrap.state is RuntimeLifecycleState.RECOVERY
        assert restarted.incidents.get_operation_result(operation_id) is not None
        assert restarted.state.resolve("EVT-REPEATED-STARTUP").processed is None

        monkeypatch.setattr(
            restarted.incident_manager, "apply_correlation_mutation", original
        )
        restarted.core.startup()
        assert restarted.bootstrap.state is RuntimeLifecycleState.READY
        assert restarted.state.resolve("EVT-REPEATED-STARTUP").processed is not None
        assert restarted.incidents.get_operation_result(operation_id) is not None
    finally:
        restarted.close()


def test_startup_expired_pending_finishes_before_ready_and_active_pending_is_preserved(
    tmp_path,
) -> None:
    expired_root = tmp_path / "startup-expired"
    event = _event("EVT-STARTUP-PENDING", "high_latency_detected", NOW)
    first = _HostRuntime(expired_root, [event])
    first.run_until_idle()
    expires_at = first.state.resolve("EVT-STARTUP-PENDING").pending.expires_at
    first.close()

    expired_time = _Time()
    expired_time.wall = expires_at
    restarted = _HostRuntime(expired_root, [event], time=expired_time)
    try:
        restarted.core.startup()
        resolved = restarted.state.resolve("EVT-STARTUP-PENDING")
        assert restarted.bootstrap.state is RuntimeLifecycleState.READY
        assert resolved.pending is None
        assert resolved.processed is not None
    finally:
        restarted.close()

    active_root = tmp_path / "startup-active"
    active_first = _HostRuntime(active_root, [event])
    active_first.run_until_idle()
    pending = active_first.state.resolve("EVT-STARTUP-PENDING").pending
    active_first.close()
    active_time = _Time()
    active_time.wall = pending.expires_at - timedelta(seconds=1)
    active = _HostRuntime(active_root, [event], time=active_time)
    try:
        active.core.startup()
        restored = active.state.resolve("EVT-STARTUP-PENDING").pending
        assert active.bootstrap.state is RuntimeLifecycleState.READY
        assert restored.entered_pending_at == pending.entered_pending_at
        assert restored.expires_at == pending.expires_at
        assert (restored.policy_id, restored.policy_version) == (
            pending.policy_id,
            pending.policy_version,
        )
    finally:
        active.close()


def test_normal_intake_cannot_cross_incomplete_recovery_barrier(tmp_path, monkeypatch) -> None:
    root = tmp_path / "barrier-order"
    events = [
        _event("EVT-RECOVERY-FIRST", "brute_force_detected", NOW, source_ip="203.0.113.82"),
        _event("EVT-LIVE-LATER", "general_log_anomaly", NOW + timedelta(seconds=1)),
    ]
    first = _HostRuntime(root, events)
    first.core.startup()
    monkeypatch.setattr(
        first.incident_manager,
        "apply_correlation_mutation",
        lambda _request: (_ for _ in ()).throw(RuntimeError("crash after Intent")),
    )
    with pytest.raises(RuntimeError):
        first.terminal.process_authoritative_event("EVT-RECOVERY-FIRST")
    first.close()

    restarted = _HostRuntime(root, events)
    original_recover = restarted.pending.recover_authoritative_intent

    def assert_barrier(event_id):
        assert restarted.bootstrap.state is RuntimeLifecycleState.RECOVERY
        with pytest.raises(RuntimeNotReadyError):
            restarted.bootstrap.dispatch_normal_intake()
        assert restarted.state.resolve("EVT-LIVE-LATER").processed is None
        return original_recover(event_id)

    monkeypatch.setattr(restarted.pending, "recover_authoritative_intent", assert_barrier)
    try:
        restarted.core.startup()
        assert restarted.state.resolve("EVT-RECOVERY-FIRST").processed is not None
        assert restarted.state.resolve("EVT-LIVE-LATER").processed is None
    finally:
        restarted.close()


def test_startup_reconstructs_zero_record_auto_assign_before_ready(tmp_path) -> None:
    root = tmp_path / "startup-zero-record"
    event = _event("EVT-ZERO-WORK", "brute_force_detected", NOW, source_ip="203.0.113.83")
    first = _HostRuntime(root, [event])
    first.core.startup()
    terminal_without_assignment = InitialCorrelationOrchestrator(
        event_intake=first.intake,
        state_store=first.state,
        policy_engine=AlertCorrelationPolicyEngine(),
        incident_store=first.incidents,
        incident_manager=first.incident_manager,
        shadow_store=first.shadows,
        shadow_manager=first.shadow_manager,
        clock=first.clock,
        readiness=first.bootstrap,
    )
    processed = terminal_without_assignment.process_authoritative_event(
        "EVT-ZERO-WORK"
    ).processed
    assert first.work.enumerate_all().records == ()
    first.close()

    restarted = _HostRuntime(root, [event])
    try:
        restarted.core.startup()
        records = restarted.work.enumerate_all().records
        assert restarted.bootstrap.state is RuntimeLifecycleState.READY
        assert len(records) == 1
        assert records[0].work_kind is RuntimeWorkKind.AUTO_ASSIGN
        assert records[0].status is RuntimeWorkStatus.COMPLETED
        assert restarted.incidents.get_incident(processed.incident_id).status is IncidentStatus.ASSIGNED
    finally:
        restarted.close()


def test_startup_restores_retry_budget_and_eligibility_before_ready(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "startup-retry"
    event = _event("EVT-RESTORE-RETRY", "brute_force_detected", NOW, source_ip="203.0.113.84")
    first = _HostRuntime(root, [event])
    first.core.startup()

    def transient(request):
        raise IncidentDomainError(
            IncidentErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE,
            "transient",
            operation_id=request.intent.operation_id,
            event_id=request.intent.event_id,
        )

    monkeypatch.setattr(first.incident_manager, "apply_correlation_mutation", transient)
    with pytest.raises(IncidentDomainError):
        first.terminal.process_authoritative_event("EVT-RESTORE-RETRY")
    scheduled = first.work.enumerate_all().records[0]
    assert scheduled.attempt_count == 1
    first.close()

    restarted = _HostRuntime(root, [event])
    try:
        restarted.core.startup()
        restored = restarted.work.get(scheduled.work_id)
        assert restarted.bootstrap.state is RuntimeLifecycleState.READY
        assert restored.attempt_count == scheduled.attempt_count
        assert restored.next_retry_at == scheduled.next_retry_at
        assert restored.operation_id == scheduled.operation_id
        assert restarted.retry.enumerate_eligibility().eligible == ()
    finally:
        restarted.close()


def test_production_claim_refusal_emits_telemetry(tmp_path, caplog) -> None:
    event = _event("EVT-CLAIM-BUSY", "brute_force_detected", NOW, source_ip="203.0.113.85")
    runtime = _HostRuntime(tmp_path / "claim-refusal", [event])
    runtime.core.startup()
    claim = runtime.state.acquire_claim("EVT-CLAIM-BUSY")
    assert claim is not None
    try:
        with caplog.at_level(logging.INFO, logger=runtime.telemetry._logger.name):
            outcome = runtime.pending.process_authoritative_event("EVT-CLAIM-BUSY")
        assert outcome.outcome_kind.value == "CLAIM_BUSY"
        assert any(
            '"claim_result":"REFUSED"' in record.message
            and '"stage":"CLAIM"' in record.message
            for record in caplog.records
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("replay_count", (1, 10, 100))
def test_host_same_logical_input_replay_is_exactly_once(tmp_path, replay_count) -> None:
    event = _event(
        "EVT-REPLAY", "brute_force_detected", NOW, source_ip="198.51.100.77"
    )
    runtime = _HostRuntime(tmp_path / f"replay-{replay_count}", [event])
    try:
        for _ in range(replay_count):
            runtime.run_until_idle()
        processed = runtime.state.resolve("EVT-REPLAY").processed
        incident = runtime.incidents.get_incident(processed.incident_id)
        assert processed.terminal_outcome is TerminalOutcome.CREATED_INCIDENT
        assert incident.event_ids == ("EVT-REPLAY",)
        assert len(runtime.incidents.list_correlation_views()) == 1
        assert len(runtime.incidents.list_workflow_audit(incident.incident_id)) == 1
        assert runtime.shadows.get_shadow_by_event_id("EVT-REPLAY") is None
    finally:
        runtime.close()


def test_host_restart_rediscovers_authority_without_duplicate_owner_or_assignment(
    tmp_path,
) -> None:
    root = tmp_path / "restart"
    event = _event(
        "EVT-RESTART", "brute_force_detected", NOW, source_ip="192.0.2.42"
    )
    time = _Time()
    first = _HostRuntime(root, [event], time=time)
    first.run_until_idle()
    processed = first.state.resolve("EVT-RESTART").processed
    incident_id = processed.incident_id
    first.close()

    restarted = _HostRuntime(root, [event], time=time)
    try:
        restarted.run_until_idle()
        replayed = restarted.state.resolve("EVT-RESTART").processed
        incident = restarted.incidents.get_incident(incident_id)
        assert replayed == processed
        assert incident.event_ids == ("EVT-RESTART",)
        assert len(restarted.incidents.list_correlation_views()) == 1
        assert len(restarted.incidents.list_workflow_audit(incident_id)) == 1
        assert restarted.work.enumerate_all().isolated_corruptions == ()
    finally:
        restarted.close()
