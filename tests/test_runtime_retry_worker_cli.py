"""SPEC-011 Phase 6 bounded retry, worker loop and CLI evidence."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import pytest
import yaml

from alert_correlation import AlertCorrelationPolicyEngine
from incident_management import (
    IncidentDomainError,
    IncidentErrorCode,
    IncidentStatus,
    WorkflowDomainError,
    WorkflowErrorCode,
)
from runtime_orchestration import (
    DurableRetryController,
    AutoAssignOrchestrator,
    AuthoritativeTerminalRetryExecutor,
    DurableEventIntake,
    InitialCorrelationOrchestrator,
    RuntimeClock,
    RuntimeCycleResult,
    RuntimeRetryIntegrityError,
    RuntimeStopController,
    RuntimeTelemetryEvent,
    RuntimeWorker,
    RuntimeWorkerExit,
    RuntimeWorkerMode,
    RuntimeWorkKind,
    RuntimeWorkRecord,
    RuntimeWorkStatus,
    SqliteRuntimeWorkStore,
    StdlibRuntimeTelemetry,
    TerminalFailureRecorder,
    runtime_work_id,
)
from runtime_orchestration.cli import build_runtime_application, main
from runtime_orchestration.config import RuntimeConfigError, load_runtime_config
from runtime_orchestration.orchestrator import StartupBarrierError
from test_runtime_auto_assign import _Harness as _AssignmentHarness
from test_runtime_auto_assign import _event as _assignment_event
from test_runtime_auto_assign import ACTOR as _ASSIGNMENT_ACTOR
from test_runtime_auto_assign import POLICY as _ASSIGNMENT_POLICY
from test_runtime_terminal_orchestration import _RuntimeHarness as _TerminalHarness
from test_runtime_terminal_orchestration import _event as _terminal_event


NOW = datetime(2026, 9, 15, 2, 0, tzinfo=timezone.utc)


class _Time:
    def __init__(self, wall=NOW) -> None:
        self.wall = wall
        self.monotonic = 10.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.monotonic += seconds
        if self.on_sleep is not None:
            self.on_sleep()

    def clock(self) -> RuntimeClock:
        return RuntimeClock(lambda: self.wall, lambda: self.monotonic, self.sleep)


def _work(controller: DurableRetryController, *, event_id="EVT-RETRY"):
    return RuntimeWorkRecord(
        work_id=runtime_work_id(RuntimeWorkKind.DOMAIN_OPERATION, event_id, "OP-STABLE"),
        work_kind=RuntimeWorkKind.DOMAIN_OPERATION,
        event_id=event_id,
        stage="DOMAIN_ATTEMPT",
        next_action="RECONCILE_OPERATION",
        operation_id="OP-STABLE",
        attempt_count=0,
        retry_limit=controller.domain_attempt_limit,
        status=RuntimeWorkStatus.OUTSTANDING,
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW,
    )


def _error(code: WorkflowErrorCode) -> WorkflowDomainError:
    return WorkflowDomainError(code, "typed domain failure")


def test_retryable_exact_1_2_4_8_then_exhaustion_preserves_domain_evidence(
    tmp_path,
) -> None:
    time = _Time()
    store = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    controller = DurableRetryController(
        work_store=store,
        retry_delays_seconds=(1, 2, 4, 8),
        clock=time.clock(),
    )
    work = store.create(_work(controller))
    expected = (1, 2, 4, 8)

    for failed_attempt, delay in enumerate(expected, start=1):
        work = controller.record_domain_failure(
            work,
            _error(WorkflowErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE),
            source_domain="SPEC-009",
        )
        assert work.attempt_count == failed_attempt
        assert work.next_retry_at == time.wall + timedelta(seconds=delay)
        assert work.operation_id == "OP-STABLE"
        time.wall = work.next_retry_at

    exhausted = controller.record_domain_failure(
        work,
        _error(WorkflowErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE),
        source_domain="SPEC-009",
    )
    assert exhausted.attempt_count == 5  # initial attempt + four retries
    assert exhausted.retry_limit == 5
    assert exhausted.status is RuntimeWorkStatus.EXHAUSTED
    assert exhausted.next_retry_at is None
    assert exhausted.source_error_code == "TRANSIENT_INCIDENT_STORE_FAILURE"
    assert exhausted.source_retry_disposition == "RETRYABLE"
    store.close()


@pytest.mark.parametrize(
    "code",
    (
        WorkflowErrorCode.INVALID_WORKFLOW_MUTATION,
        WorkflowErrorCode.INCIDENT_WORKFLOW_INTEGRITY_FAILURE,
    ),
)
def test_non_retryable_and_repair_required_never_enter_automatic_retry(
    tmp_path, code
) -> None:
    store = SqliteRuntimeWorkStore(tmp_path / f"{code.value}.sqlite3")
    controller = DurableRetryController(
        work_store=store, retry_delays_seconds=(1, 2, 4, 8), clock=_Time().clock()
    )
    failed = controller.record_domain_failure(
        store.create(_work(controller, event_id=f"EVT-{code.value}")),
        _error(code),
        source_domain="SPEC-009",
    )

    assert failed.status is RuntimeWorkStatus.FAILED_CLOSED
    assert failed.next_retry_at is None
    assert failed.source_retry_disposition == _error(code).retry_disposition.value
    assert controller.enumerate_eligibility().eligible == ()
    store.close()


def test_restart_preserves_budget_eligibility_and_stable_operation_id(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite3"
    time = _Time()
    store = SqliteRuntimeWorkStore(path)
    controller = DurableRetryController(
        work_store=store, retry_delays_seconds=(1, 2, 4, 8), clock=time.clock()
    )
    first = controller.record_domain_failure(
        store.create(_work(controller)),
        _error(WorkflowErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE),
        source_domain="SPEC-009",
    )
    store.close()

    time.wall = NOW + timedelta(milliseconds=999)
    reopened = SqliteRuntimeWorkStore(path)
    restarted = DurableRetryController(
        work_store=reopened, retry_delays_seconds=(1, 2, 4, 8), clock=time.clock()
    )
    before = restarted.enumerate_eligibility()
    assert before.eligible == ()
    assert before.next_eligibility == NOW + timedelta(seconds=1)

    time.wall = datetime(2026, 9, 15, 10, 0, 1, tzinfo=timezone(timedelta(hours=8)))
    due = restarted.enumerate_eligibility()
    assert due.eligible == (reopened.get(first.work_id),)
    second = restarted.record_domain_failure(
        due.eligible[0],
        _error(WorkflowErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE),
        source_domain="SPEC-009",
    )
    assert second.attempt_count == 2
    assert second.operation_id == first.operation_id == "OP-STABLE"
    assert second.next_retry_at == NOW + timedelta(seconds=3)
    reopened.close()


def test_retry_controller_rejects_local_or_string_based_taxonomy(tmp_path) -> None:
    class LocalCode(str, Enum):
        TEMP = "TEMP"

    class GuessedFailure:
        code = LocalCode.TEMP
        retry_disposition = "RETRYABLE"

    store = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    controller = DurableRetryController(
        work_store=store, retry_delays_seconds=(1, 2, 4, 8), clock=_Time().clock()
    )
    with pytest.raises(RuntimeRetryIntegrityError, match="RetryDisposition"):
        controller.record_domain_failure(
            store.create(_work(controller)),
            GuessedFailure(),
            source_domain="LOCAL",
        )
    store.close()


def test_auto_assign_uses_four_retry_delays_and_same_workflow_id(tmp_path) -> None:
    h = _AssignmentHarness(
        tmp_path, [_assignment_event("EVT-AUTO-RETRY", "203.0.113.201")]
    )
    processed = h.create_processed_open("EVT-AUTO-RETRY")

    class AlwaysTransient:
        def __init__(self):
            self.operation_ids = []

        def auto_assign_incident(self, request, _policy):
            self.operation_ids.append(request.workflow_operation_id)
            raise _error(WorkflowErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE)

    failing = AlwaysTransient()
    assignment = AutoAssignOrchestrator(
        state_store=h.state,
        incident_store=h.incidents,
        incident_manager=failing,
        work_store=h.work,
        assignment_policy=_ASSIGNMENT_POLICY,
        automation_actor=_ASSIGNMENT_ACTOR,
        retry_delays_seconds=(1, 2, 4, 8),
        clock=h.clock,
    )
    try:
        for delay in (1, 2, 4, 8):
            with pytest.raises(WorkflowDomainError):
                assignment.handle_processed_created_incident(processed)
            work = h.work.enumerate_all().records[0]
            assert work.next_retry_at == h.now.value + timedelta(seconds=delay)
            h.now.value = work.next_retry_at
        with pytest.raises(WorkflowDomainError):
            assignment.handle_processed_created_incident(processed)
        exhausted = h.work.enumerate_all().records[0]
        assert exhausted.status is RuntimeWorkStatus.EXHAUSTED
        assert exhausted.attempt_count == exhausted.retry_limit == 5
        assert len(failing.operation_ids) == 5
        assert len(set(failing.operation_ids)) == 1
    finally:
        h.close()


def test_auto_assign_drain_stops_before_acquiring_next_obligation(tmp_path) -> None:
    events = [
        _assignment_event("EVT-DRAIN-A", "203.0.113.202"),
        _assignment_event("EVT-DRAIN-B", "203.0.113.203"),
    ]
    h = _AssignmentHarness(tmp_path, events)
    first = h.create_processed_open("EVT-DRAIN-A")
    second = h.create_processed_open("EVT-DRAIN-B")
    stop = RuntimeStopController()
    original = h.manager.auto_assign_incident

    class StopAfterFirst:
        def auto_assign_incident(self, request, policy):
            result = original(request, policy)
            stop.request_stop()
            return result

    try:
        result = h.make_assignment(manager=StopAfterFirst()).recover_all(
            should_stop=lambda: stop.requested
        )
        assert len(result.outcomes) == 1
        assert h.incidents.get_incident(first.incident_id).status is IncidentStatus.ASSIGNED
        assert h.incidents.get_incident(second.incident_id).status is IncidentStatus.OPEN
        assert len(h.work.enumerate_all().records) == 1
    finally:
        h.close()


def test_terminal_retry_fresh_resolves_same_intent_and_completes_d2(
    tmp_path, monkeypatch
) -> None:
    h = _TerminalHarness(
        tmp_path, [_terminal_event("EVT-TERMINAL-RETRY", "brute_force_detected", NOW, source_ip="203.0.113.204")]
    )
    time = _Time()
    work_store = SqliteRuntimeWorkStore(tmp_path / "retry.sqlite3")
    retry = DurableRetryController(
        work_store=work_store,
        retry_delays_seconds=(1, 2, 4, 8),
        clock=time.clock(),
    )
    recorder = TerminalFailureRecorder(
        work_store=work_store, retry=retry, clock=time.clock()
    )
    terminal = InitialCorrelationOrchestrator(
        event_intake=DurableEventIntake(h.events),
        state_store=h.state,
        policy_engine=AlertCorrelationPolicyEngine(),
        incident_store=h.incidents,
        incident_manager=h.incident_manager,
        shadow_store=h.shadows,
        shadow_manager=h.shadow_manager,
        clock=time.clock(),
        readiness=h.readiness,
        terminal_failure_recorder=recorder,
    )
    original = h.incident_manager.apply_correlation_mutation

    def transient(request):
        raise IncidentDomainError(
            IncidentErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE,
            "transient",
            operation_id=request.intent.operation_id,
            event_id=request.intent.event_id,
        )

    monkeypatch.setattr(h.incident_manager, "apply_correlation_mutation", transient)
    try:
        with pytest.raises(IncidentDomainError):
            terminal.process_authoritative_event("EVT-TERMINAL-RETRY")
        scheduled = work_store.enumerate_all().records[0]
        resolved = h.state.resolve("EVT-TERMINAL-RETRY")
        assert scheduled.operation_id == resolved.intent.operation_id
        assert scheduled.attempt_count == 1
        assert retry.enumerate_eligibility().eligible == ()

        time.wall = scheduled.next_retry_at
        monkeypatch.setattr(h.incident_manager, "apply_correlation_mutation", original)
        executor = AuthoritativeTerminalRetryExecutor(
            state_store=h.state,
            terminal_executor=terminal,
            work_store=work_store,
            clock=time.clock(),
        )
        eligible = retry.enumerate_eligibility().eligible
        def commit_then_lose_response(request):
            original(request)
            raise RuntimeError("domain committed before Runtime bookkeeping")

        monkeypatch.setattr(
            h.incident_manager,
            "apply_correlation_mutation",
            commit_then_lose_response,
        )
        with pytest.raises(RuntimeError, match="committed"):
            executor.execute_retry(eligible[0])
        assert h.incidents.get_operation_result(scheduled.operation_id) is not None
        assert h.state.resolve("EVT-TERMINAL-RETRY").processed is None
        assert work_store.get(scheduled.work_id).status is RuntimeWorkStatus.OUTSTANDING

        monkeypatch.setattr(h.incident_manager, "apply_correlation_mutation", original)
        completed = executor.execute_retry(eligible[0])
        final = h.state.resolve("EVT-TERMINAL-RETRY")

        assert completed.status is RuntimeWorkStatus.COMPLETED
        assert final.processed is not None
        assert h.incidents.get_operation_result(scheduled.operation_id) is not None
    finally:
        work_store.close()
        h.close()


class _Core:
    def __init__(self, results, *, stop=None, durable_stage=None) -> None:
        self.results = list(results)
        self.stop = stop
        self.durable_stage = durable_stage
        self.started = 0
        self.cycles = 0
        self.boundaries: list[str] = []

    def startup(self) -> None:
        self.started += 1

    def run_cycle(self) -> RuntimeCycleResult:
        self.cycles += 1
        if self.durable_stage is not None:
            self.boundaries.append(f"{self.durable_stage}:started")
            self.stop.request_stop()
            self.boundaries.append(f"{self.durable_stage}:durable-safe-boundary")
        result = self.results.pop(0)
        if self.stop is not None and not self.results:
            self.stop.request_stop()
        return result


def test_continuous_and_run_until_idle_share_identical_core_semantics() -> None:
    sequence = [RuntimeCycleResult(1, 1), RuntimeCycleResult(0, 0)]
    idle_core = _Core(sequence)
    idle_worker = RuntimeWorker(
        idle_core, clock=_Time().clock(), idle_poll_seconds=1
    )
    assert idle_worker.run(RuntimeWorkerMode.RUN_UNTIL_IDLE) is RuntimeWorkerExit.IDLE

    stop = RuntimeStopController()
    continuous_core = _Core(sequence, stop=stop)
    continuous_worker = RuntimeWorker(
        continuous_core,
        clock=_Time().clock(),
        idle_poll_seconds=1,
        stop=stop,
    )
    assert continuous_worker.run(RuntimeWorkerMode.CONTINUOUS) is RuntimeWorkerExit.DRAINED
    assert idle_core.cycles == continuous_core.cycles == 2
    assert idle_core.started == continuous_core.started == 1


@pytest.mark.parametrize("stage", ("claim", "Intent", "retry", "AUTO_ASSIGN"))
def test_controlled_stop_finishes_acquired_work_at_durable_safe_boundary(stage) -> None:
    stop = RuntimeStopController()
    core = _Core(
        [RuntimeCycleResult(1, 1)], stop=stop, durable_stage=stage
    )
    time = _Time()
    worker = RuntimeWorker(
        core, clock=time.clock(), idle_poll_seconds=1, stop=stop
    )

    assert worker.run(RuntimeWorkerMode.CONTINUOUS) is RuntimeWorkerExit.DRAINED
    assert core.cycles == 1
    assert core.boundaries == [f"{stage}:started", f"{stage}:durable-safe-boundary"]
    assert time.sleeps == []


def test_continuous_wait_uses_injected_monotonic_clock_and_fake_sleeper() -> None:
    stop = RuntimeStopController()
    core = _Core([RuntimeCycleResult(0, 0)], stop=None)
    time = _Time()
    time.on_sleep = stop.request_stop
    worker = RuntimeWorker(
        core, clock=time.clock(), idle_poll_seconds=3, stop=stop
    )

    assert worker.run(RuntimeWorkerMode.CONTINUOUS) is RuntimeWorkerExit.DRAINED
    assert time.sleeps == [3.0]


def test_startup_failure_is_fail_fast_and_never_runs_cycle() -> None:
    core = _Core([])

    def fail():
        raise StartupBarrierError("unreadable authority")

    core.startup = fail
    worker = RuntimeWorker(core, clock=_Time().clock(), idle_poll_seconds=1)
    with pytest.raises(StartupBarrierError):
        worker.run(RuntimeWorkerMode.RUN_UNTIL_IDLE)
    assert core.cycles == 0


def test_config_validates_loop_and_authority_store_paths(tmp_path) -> None:
    raw = yaml.safe_load(Path("configs/runtime_orchestration.yaml").read_text(encoding="utf-8"))
    raw["loop"]["mode"] = "different-business-core"
    path = tmp_path / "bad-mode.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match="loop.mode"):
        load_runtime_config(path)

    raw["loop"]["mode"] = "run-until-idle"
    raw["authority_stores"]["incident_store_path"] = "../private.sqlite3"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match="repo-relative"):
        load_runtime_config(path)


def test_real_runtime_application_empty_store_runs_until_idle(tmp_path) -> None:
    config_path = Path("configs/runtime_orchestration.yaml").resolve()
    config = load_runtime_config(config_path)
    application = build_runtime_application(
        config, project_root=tmp_path, config_path=config_path
    )
    try:
        assert application.run(RuntimeWorkerMode.RUN_UNTIL_IDLE) is RuntimeWorkerExit.IDLE
    finally:
        application.close()


def test_real_runtime_application_corrupt_event_store_fails_bootstrap(tmp_path) -> None:
    config_path = Path("configs/runtime_orchestration.yaml").resolve()
    config = load_runtime_config(config_path)
    event_path = tmp_path / config.authority_stores.event_store_path
    event_path.parent.mkdir(parents=True)
    event_path.write_text("{broken", encoding="utf-8")
    application = build_runtime_application(
        config, project_root=tmp_path, config_path=config_path
    )
    try:
        with pytest.raises(StartupBarrierError):
            application.run(RuntimeWorkerMode.RUN_UNTIL_IDLE)
    finally:
        application.close()


def test_cli_mode_is_config_driven_and_application_is_closed(tmp_path) -> None:
    raw = yaml.safe_load(Path("configs/runtime_orchestration.yaml").read_text(encoding="utf-8"))
    raw["loop"]["mode"] = "continuous"
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    observed = {}

    class App:
        def __init__(self):
            self.worker = type("WorkerRef", (), {"stop_controller": RuntimeStopController()})()

        def run(self, mode):
            observed["mode"] = mode

        def close(self):
            observed["closed"] = True

    assert main(
        ["--config", str(path)],
        application_factory=lambda *_args, **_kwargs: App(),
    ) == 0
    assert observed == {"mode": RuntimeWorkerMode.CONTINUOUS, "closed": True}


def test_cli_store_open_failure_is_not_silently_recovered(tmp_path) -> None:
    config = load_runtime_config("configs/runtime_orchestration.yaml")
    (tmp_path / "var").write_text("not a directory", encoding="utf-8")
    with pytest.raises((FileExistsError, NotADirectoryError, OSError)):
        build_runtime_application(
            config,
            project_root=tmp_path,
            config_path=Path("configs/runtime_orchestration.yaml").resolve(),
        )


def test_structured_telemetry_supports_required_retry_and_trace_fields(caplog) -> None:
    logger = logging.getLogger("runtime.phase6.telemetry")
    telemetry = StdlibRuntimeTelemetry(logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        telemetry.emit(
            RuntimeTelemetryEvent.RETRY_SCHEDULED,
            observed_at=NOW,
            event_id="EVT-1",
            stage="RETRY_PENDING",
            decision="CREATE_NEW",
            policy_id="POLICY-1",
            policy_version="1.0",
            operation_id="OP-1",
            workflow_operation_id="WF-1",
            claim_result="ACQUIRED",
            attempt=2,
            retry_disposition="RETRYABLE",
            next_eligibility=NOW + timedelta(seconds=2),
            incident_id="INC-1",
            reconciliation_result="PENDING",
            source_domain="SPEC-009",
            source_error_code="TRANSIENT_INCIDENT_STORE_FAILURE",
        )
    payload = json.loads(caplog.records[-1].message)
    assert payload["runtime_event"] == "RETRY_SCHEDULED"
    assert payload["attempt"] == 2
    assert payload["next_eligibility"].endswith("Z")
    assert "event_payload" not in payload


def test_retry_controller_production_path_emits_durable_retry_evidence(
    tmp_path, caplog
) -> None:
    logger = logging.getLogger("runtime.phase6.production-retry")
    telemetry = StdlibRuntimeTelemetry(logger)
    store = SqliteRuntimeWorkStore(tmp_path / "telemetry-retry.sqlite3")
    controller = DurableRetryController(
        work_store=store,
        retry_delays_seconds=(1, 2, 4, 8),
        clock=_Time().clock(),
        telemetry=telemetry,
    )
    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            controller.record_domain_failure(
                store.create(_work(controller, event_id="EVT-TELEMETRY-RETRY")),
                _error(WorkflowErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE),
                source_domain="SPEC-009",
            )
        payload = json.loads(caplog.records[-1].message)
        assert payload["runtime_event"] == "RETRY_SCHEDULED"
        assert payload["event_id"] == "EVT-TELEMETRY-RETRY"
        assert payload["attempt"] == 1
        assert payload["retry_disposition"] == "RETRYABLE"
        assert payload["next_eligibility"].endswith("Z")
        assert payload["source_domain"] == "SPEC-009"
    finally:
        store.close()
