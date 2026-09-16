"""SPEC-011 Phase 5 AUTO_ASSIGN obligation and zero-record recovery."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from alert_correlation import AlertCorrelationPolicyEngine
from alert_correlation.policy import DEFAULT_POLICY_REGISTRY
from alert_correlation.state import SqliteCorrelationStateStore, TerminalOutcome
from incident_management import (
    AssignmentPolicyConfig,
    IncidentManager,
    IncidentStatus,
    SqliteIncidentStore,
    WorkflowAction,
    WorkflowDomainError,
    WorkflowErrorCode,
    WorkflowMutationRequest,
)
from runtime_orchestration import (
    AutoAssignIntegrityError,
    AutoAssignOrchestrator,
    AutoAssignOutcomeKind,
    DurableEventIntake,
    InitialCorrelationOrchestrator,
    RuntimeClock,
    RuntimeLifecycleState,
    RuntimeWorkKind,
    RuntimeWorkRecord,
    RuntimeWorkStatus,
    SqliteRuntimeWorkStore,
    automatic_assignment_operation_id,
    runtime_work_id,
)
from shadow_management import ShadowManager, SqliteShadowStore


NOW = datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)
POLICY = AssignmentPolicyConfig(
    "POC-ROUND-ROBIN", "1.0", ("engineer-a", "engineer-b"), "supervisor"
)
ACTOR = "runtime-auto-assign"


def _event(event_id: str, source_ip: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "detected_at": NOW.isoformat().replace("+00:00", "Z"),
        "event_source": "log_event_detection",
        "event_type": "brute_force_detected",
        "detection_method": "rule_based",
        "severity": "HIGH",
        "confidence": 0.99,
        "service_name": "auth-api",
        "trace_id": None,
        "source_ip": source_ip,
        "downstream_service": None,
        "external_service": None,
        "status": "OPEN",
        "triggered_features": {},
        "raw_log_sample": [],
    }


class _Events:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events

    def read_all_authoritative(self) -> list[dict[str, object]]:
        return self.events


class _Ready:
    state = RuntimeLifecycleState.READY


class _MutableNow:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


class _Harness:
    def __init__(self, tmp_path, events: list[dict[str, object]]) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.root = tmp_path
        self.now = _MutableNow()
        self.clock = RuntimeClock(self.now, lambda: 0.0, lambda _seconds: None)
        self.events = _Events(events)
        self.state = SqliteCorrelationStateStore(tmp_path / "state.sqlite3")
        self.incidents = SqliteIncidentStore(str(tmp_path / "incidents.sqlite3"))
        self.shadows = SqliteShadowStore(tmp_path / "shadows.sqlite3")
        self.work = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
        ids = count(1)
        self.manager = IncidentManager(
            self.incidents, incident_id_factory=lambda: f"INC-{next(ids)}"
        )
        self.assignment = self.make_assignment()

    def restart_durable_authorities(self) -> None:
        self.state.close()
        self.incidents.close()
        self.work.close()
        self.state = SqliteCorrelationStateStore(self.root / "state.sqlite3")
        self.incidents = SqliteIncidentStore(str(self.root / "incidents.sqlite3"))
        self.work = SqliteRuntimeWorkStore(self.root / "runtime.sqlite3")
        self.manager = IncidentManager(self.incidents)
        self.assignment = self.make_assignment()

    def make_assignment(self, *, manager=None, work_store=None) -> AutoAssignOrchestrator:
        return AutoAssignOrchestrator(
            state_store=self.state,
            incident_store=self.incidents,
            incident_manager=manager or self.manager,
            work_store=work_store or self.work,
            assignment_policy=POLICY,
            automation_actor=ACTOR,
            retry_delays_seconds=(1.0, 2.0, 4.0),
            clock=self.clock,
        )

    def terminal(self, *, auto=True) -> InitialCorrelationOrchestrator:
        return InitialCorrelationOrchestrator(
            event_intake=DurableEventIntake(self.events),
            state_store=self.state,
            policy_engine=AlertCorrelationPolicyEngine(),
            incident_store=self.incidents,
            incident_manager=self.manager,
            shadow_store=self.shadows,
            shadow_manager=ShadowManager(
                self.shadows, DEFAULT_POLICY_REGISTRY, self.incidents
            ),
            clock=self.clock,
            readiness=_Ready(),
            post_create_obligation=self.assignment if auto else None,
        )

    def create_processed_open(self, event_id: str):
        result = self.terminal(auto=False).process_authoritative_event(event_id)
        assert result.processed.terminal_outcome is TerminalOutcome.CREATED_INCIDENT
        return result.processed

    def close(self) -> None:
        self.state.close()
        self.incidents.close()
        self.shadows.close()
        self.work.close()


@pytest.fixture
def harness(tmp_path):
    opened: list[_Harness] = []

    def create(events):
        item = _Harness(tmp_path / f"case-{len(opened)}", events)
        opened.append(item)
        return item

    yield create
    for item in opened:
        item.close()


def _work(h: _Harness, processed):
    return h.work.get(
        runtime_work_id(
            RuntimeWorkKind.AUTO_ASSIGN, processed.event_id, processed.incident_id
        )
    )


def test_repair_required_auto_assign_is_isolated_from_unrelated_recovery(harness) -> None:
    events = [
        _event("EVT-REPAIR-ISOLATED", "203.0.113.90"),
        _event("EVT-SAFE-RECOVERY", "203.0.113.91"),
    ]
    h = harness(events)
    repair_processed = h.create_processed_open("EVT-REPAIR-ISOLATED")
    safe_processed = h.create_processed_open("EVT-SAFE-RECOVERY")

    class RepairRequiredManager:
        def auto_assign_incident(self, request, _policy):
            raise WorkflowDomainError(
                WorkflowErrorCode.INCIDENT_WORKFLOW_INTEGRITY_FAILURE,
                "operator repair required",
                workflow_operation_id=request.workflow_operation_id,
                incident_id=request.incident_id,
            )

    with pytest.raises(WorkflowDomainError):
        h.make_assignment(manager=RepairRequiredManager()).handle_processed_created_incident(
            repair_processed
        )
    failed = _work(h, repair_processed)
    assert failed.status is RuntimeWorkStatus.FAILED_CLOSED

    recovered = h.assignment.recover_all()
    by_event = {item.event_id: item for item in recovered.outcomes}
    assert by_event["EVT-REPAIR-ISOLATED"].outcome_kind is AutoAssignOutcomeKind.TERMINAL_FAILURE_PRESERVED
    assert by_event["EVT-REPAIR-ISOLATED"].work == failed
    assert by_event["EVT-SAFE-RECOVERY"].outcome_kind is AutoAssignOutcomeKind.ASSIGNED
    assert h.incidents.get_incident(repair_processed.incident_id).status is IncidentStatus.OPEN
    assert h.incidents.get_incident(safe_processed.incident_id).status is IncidentStatus.ASSIGNED
    assert h.work.get(failed.work_id).status is RuntimeWorkStatus.FAILED_CLOSED

    repeated = h.assignment.recover_all()
    repeated_by_event = {item.event_id: item for item in repeated.outcomes}
    assert repeated_by_event["EVT-REPAIR-ISOLATED"].outcome_kind is AutoAssignOutcomeKind.TERMINAL_FAILURE_PRESERVED
    assert h.work.get(failed.work_id).attempt_count == failed.attempt_count


def test_create_new_establishes_processed_then_durable_work_before_auto_assign(
    harness, monkeypatch
) -> None:
    h = harness([_event("EVT-1", "203.0.113.1")])
    original = h.manager.auto_assign_incident

    def asserting_call(request, policy):
        resolved = h.state.resolve("EVT-1")
        work = h.work.enumerate_all().records
        assert resolved.processed is not None
        assert resolved.claim is None
        assert len(work) == 1
        assert work[0].status is RuntimeWorkStatus.OUTSTANDING
        assert work[0].workflow_operation_id == request.workflow_operation_id
        return original(request, policy)

    monkeypatch.setattr(h.manager, "auto_assign_incident", asserting_call)
    processed = h.terminal().process_authoritative_event("EVT-1").processed
    incident = h.incidents.get_incident(processed.incident_id)
    work = _work(h, processed)

    assert incident.status is IncidentStatus.ASSIGNED
    assert incident.assignee == "engineer-a"
    assert work.status is RuntimeWorkStatus.COMPLETED
    assert len(h.incidents.list_workflow_audit(incident.incident_id)) == 1
    assert h.incidents.get_assignment_state(POLICY.policy_id, POLICY.policy_version)[-1] == 1


def test_zero_record_recovery_reconstructs_exactly_once_and_restart_is_replay_safe(
    harness,
) -> None:
    h = harness([_event("EVT-ZERO", "203.0.113.2")])
    processed = h.create_processed_open("EVT-ZERO")
    assert _work(h, processed) is None

    h.restart_durable_authorities()
    first = h.assignment.recover_all()
    h.restart_durable_authorities()
    second = h.assignment.recover_all()
    incident_id = processed.incident_id

    assert first.outcomes[0].outcome_kind is AutoAssignOutcomeKind.ASSIGNED
    assert second.outcomes[0].outcome_kind is AutoAssignOutcomeKind.RECEIPT_RECONCILED
    assert len(h.work.enumerate_all().records) == 1
    assert len(h.incidents.list_workflow_audit(incident_id)) == 1
    assert h.incidents.get_assignment_state(POLICY.policy_id, POLICY.policy_version)[-1] == 1


class _CommitThenLoseResponse:
    def __init__(self, manager: IncidentManager) -> None:
        self.manager = manager
        self.calls = 0

    def auto_assign_incident(self, request, policy):
        self.calls += 1
        self.manager.auto_assign_incident(request, policy)
        raise RuntimeError("simulated lost workflow response")


def test_workflow_commit_runtime_completion_lost_reconciles_receipt_without_duplicate(
    harness,
) -> None:
    h = harness([_event("EVT-LOST", "203.0.113.3")])
    processed = h.create_processed_open("EVT-LOST")
    lossy = _CommitThenLoseResponse(h.manager)

    with pytest.raises(RuntimeError, match="lost workflow response"):
        h.make_assignment(manager=lossy).handle_processed_created_incident(processed)
    outstanding = _work(h, processed)
    assert outstanding.status is RuntimeWorkStatus.OUTSTANDING

    outcome = h.assignment.handle_processed_created_incident(processed)
    assert outcome.outcome_kind is AutoAssignOutcomeKind.RECEIPT_RECONCILED
    assert _work(h, processed).status is RuntimeWorkStatus.COMPLETED
    assert len(h.incidents.list_workflow_audit(processed.incident_id)) == 1
    assert h.incidents.get_assignment_state(POLICY.policy_id, POLICY.policy_version)[-1] == 1


def test_receipt_reconciliation_accepts_legal_forward_lifecycle(harness) -> None:
    h = harness([_event("EVT-FORWARD", "203.0.113.31")])
    processed = h.create_processed_open("EVT-FORWARD")
    assigned = h.assignment.handle_processed_created_incident(processed)
    incident = h.incidents.get_incident(processed.incident_id)
    cursor = h.incidents.get_assignment_state(POLICY.policy_id, POLICY.policy_version)
    h.manager.start_work(
        WorkflowMutationRequest(
            "start-after-auto",
            processed.incident_id,
            incident.assignee,
            h.clock.now(),
            WorkflowAction.START_WORK,
        )
    )

    outcome = h.assignment.recover_all().outcomes[0]
    audits = h.incidents.list_workflow_audit(processed.incident_id)

    assert outcome.outcome_kind is AutoAssignOutcomeKind.RECEIPT_RECONCILED
    assert h.incidents.get_incident(processed.incident_id).status is IncidentStatus.IN_PROGRESS
    assert h.incidents.get_workflow_operation_result(
        assigned.workflow_operation_id
    ) == assigned.workflow_result
    assert sum(audit.action is WorkflowAction.AUTO_ASSIGN for audit in audits) == 1
    assert len(audits) == 2
    assert h.incidents.get_assignment_state(
        POLICY.policy_id, POLICY.policy_version
    ) == cursor
    assert _work(h, processed).status is RuntimeWorkStatus.COMPLETED


def test_forward_lifecycle_receipt_reconciliation_survives_repeated_reopen(
    harness,
) -> None:
    h = harness([_event("EVT-FORWARD-REOPEN", "203.0.113.32")])
    processed = h.create_processed_open("EVT-FORWARD-REOPEN")
    h.assignment.handle_processed_created_incident(processed)
    incident = h.incidents.get_incident(processed.incident_id)
    h.manager.start_work(
        WorkflowMutationRequest(
            "start-before-reopen",
            processed.incident_id,
            incident.assignee,
            h.clock.now(),
            WorkflowAction.START_WORK,
        )
    )
    cursor = h.incidents.get_assignment_state(POLICY.policy_id, POLICY.policy_version)

    h.restart_durable_authorities()
    first = h.assignment.recover_all().outcomes[0]
    h.restart_durable_authorities()
    second = h.assignment.recover_all().outcomes[0]
    audits = h.incidents.list_workflow_audit(processed.incident_id)

    assert first.outcome_kind is AutoAssignOutcomeKind.RECEIPT_RECONCILED
    assert second.outcome_kind is AutoAssignOutcomeKind.RECEIPT_RECONCILED
    assert sum(audit.action is WorkflowAction.AUTO_ASSIGN for audit in audits) == 1
    assert len(audits) == 2
    assert h.incidents.get_assignment_state(
        POLICY.policy_id, POLICY.policy_version
    ) == cursor
    assert _work(h, processed).status is RuntimeWorkStatus.COMPLETED


def test_receipt_reconciliation_accepts_legal_post_auto_reassignment(harness) -> None:
    h = harness([_event("EVT-REASSIGN", "203.0.113.33")])
    processed = h.create_processed_open("EVT-REASSIGN")
    assigned = h.assignment.handle_processed_created_incident(processed)
    cursor = h.incidents.get_assignment_state(POLICY.policy_id, POLICY.policy_version)
    h.manager.reassign_incident(
        WorkflowMutationRequest(
            "reassign-after-auto",
            processed.incident_id,
            "operator",
            h.clock.now(),
            WorkflowAction.REASSIGN,
            "engineer-b",
        ),
        POLICY,
    )

    outcome = h.assignment.recover_all().outcomes[0]
    incident = h.incidents.get_incident(processed.incident_id)
    audits = h.incidents.list_workflow_audit(processed.incident_id)

    assert outcome.outcome_kind is AutoAssignOutcomeKind.RECEIPT_RECONCILED
    assert incident.assignee == "engineer-b"
    assert h.incidents.get_workflow_operation_result(
        assigned.workflow_operation_id
    ) == assigned.workflow_result
    assert sum(audit.action is WorkflowAction.AUTO_ASSIGN for audit in audits) == 1
    assert len(audits) == 2
    assert h.incidents.get_assignment_state(
        POLICY.policy_id, POLICY.policy_version
    ) == cursor


class _ManualAssignmentRaceStore:
    def __init__(self, delegate, manager, processed, clock) -> None:
        self.delegate = delegate
        self.manager = manager
        self.processed = processed
        self.clock = clock

    def create(self, record):
        created = self.delegate.create(record)
        self.manager.assign_incident(
            WorkflowMutationRequest(
                "manual-race",
                self.processed.incident_id,
                "operator",
                self.clock.now(),
                WorkflowAction.MANUAL_ASSIGN,
                "engineer-b",
            ),
            POLICY,
        )
        return created

    def get(self, work_id):
        return self.delegate.get(work_id)

    def enumerate_all(self):
        return self.delegate.enumerate_all()

    def update(self, record, *, expected_revision):
        return self.delegate.update(record, expected_revision=expected_revision)

    def complete(self, work_id, *, observed_at, expected_revision=None):
        return self.delegate.complete(
            work_id, observed_at=observed_at, expected_revision=expected_revision
        )


def test_manual_assignment_race_wins_and_never_advances_auto_cursor(harness) -> None:
    h = harness([_event("EVT-MANUAL", "203.0.113.4")])
    processed = h.create_processed_open("EVT-MANUAL")
    race_store = _ManualAssignmentRaceStore(h.work, h.manager, processed, h.clock)

    outcome = h.make_assignment(work_store=race_store).handle_processed_created_incident(
        processed
    )
    incident = h.incidents.get_incident(processed.incident_id)

    assert outcome.outcome_kind is AutoAssignOutcomeKind.SUPPRESSED_DOMAIN_ADVANCED
    assert (incident.status, incident.assignee) == (
        IncidentStatus.ASSIGNED,
        "engineer-b",
    )
    assert h.incidents.get_assignment_state(POLICY.policy_id, POLICY.policy_version) is None
    assert h.incidents.get_workflow_operation_result(
        automatic_assignment_operation_id(processed.incident_id, ACTOR)
    ) is None
    assert len(h.incidents.list_workflow_audit(processed.incident_id)) == 1


def test_advanced_lifecycle_suppresses_missing_work_reconstruction(harness) -> None:
    h = harness([_event("EVT-ADVANCED", "203.0.113.5")])
    processed = h.create_processed_open("EVT-ADVANCED")
    h.manager.assign_incident(
        WorkflowMutationRequest(
            "manual-advanced",
            processed.incident_id,
            "operator",
            h.clock.now(),
            WorkflowAction.MANUAL_ASSIGN,
            "engineer-a",
        ),
        POLICY,
    )
    h.manager.start_work(
        WorkflowMutationRequest(
            "start-advanced",
            processed.incident_id,
            "engineer-a",
            h.clock.now(),
            WorkflowAction.START_WORK,
        )
    )

    outcome = h.assignment.recover_all().outcomes[0]
    assert outcome.outcome_kind is AutoAssignOutcomeKind.SUPPRESSED_DOMAIN_ADVANCED
    assert h.incidents.get_incident(processed.incident_id).status is IncidentStatus.IN_PROGRESS
    assert _work(h, processed) is None
    assert h.incidents.get_assignment_state(POLICY.policy_id, POLICY.policy_version) is None


class _TransientOnce:
    def __init__(self, manager: IncidentManager) -> None:
        self.manager = manager
        self.requests = []

    def auto_assign_incident(self, request, policy):
        self.requests.append(request)
        if len(self.requests) == 1:
            raise WorkflowDomainError(
                WorkflowErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE,
                "transient",
                workflow_operation_id=request.workflow_operation_id,
                incident_id=request.incident_id,
            )
        return self.manager.auto_assign_incident(request, policy)


def test_transient_failure_leaves_open_and_retry_uses_same_workflow_identity(
    harness,
) -> None:
    h = harness([_event("EVT-RETRY", "203.0.113.6")])
    processed = h.create_processed_open("EVT-RETRY")
    flaky = _TransientOnce(h.manager)
    assignment = h.make_assignment(manager=flaky)

    with pytest.raises(WorkflowDomainError) as raised:
        assignment.handle_processed_created_incident(processed)
    assert raised.value.retry_disposition.value == "RETRYABLE"
    failed = _work(h, processed)
    assert failed.attempt_count == 1
    assert failed.status is RuntimeWorkStatus.OUTSTANDING
    assert failed.next_retry_at == NOW + timedelta(seconds=1)
    assert h.incidents.get_incident(processed.incident_id).status is IncidentStatus.OPEN

    h.now.value = NOW + timedelta(seconds=1)
    assignment.handle_processed_created_incident(processed)
    assert flaky.requests[0].workflow_operation_id == flaky.requests[1].workflow_operation_id
    assert _work(h, processed).attempt_count == 1
    assert _work(h, processed).status is RuntimeWorkStatus.COMPLETED


def test_receipt_identity_conflict_fails_closed(harness) -> None:
    h = harness([_event("EVT-CONFLICT", "203.0.113.7")])
    processed = h.create_processed_open("EVT-CONFLICT")
    operation_id = automatic_assignment_operation_id(processed.incident_id, ACTOR)
    h.manager.assign_incident(
        WorkflowMutationRequest(
            operation_id,
            processed.incident_id,
            "operator",
            h.clock.now(),
            WorkflowAction.MANUAL_ASSIGN,
            "engineer-a",
        ),
        POLICY,
    )

    with pytest.raises(AutoAssignIntegrityError, match="receipt contradicts"):
        h.assignment.handle_processed_created_incident(processed)
    assert h.incidents.get_assignment_state(POLICY.policy_id, POLICY.policy_version) is None


def test_success_receipt_with_impossible_open_incident_state_fails_closed(
    harness, monkeypatch
) -> None:
    h = harness([_event("EVT-STATE-CONFLICT", "203.0.113.71")])
    processed = h.create_processed_open("EVT-STATE-CONFLICT")
    h.assignment.handle_processed_created_incident(processed)
    persisted = h.incidents.get_incident(processed.incident_id)
    impossible = replace(
        persisted,
        status=IncidentStatus.OPEN,
        assignee=None,
        reviewer=None,
    )
    monkeypatch.setattr(h.incidents, "get_incident", lambda _incident_id: impossible)

    with pytest.raises(AutoAssignIntegrityError, match="Incident state"):
        h.assignment.handle_processed_created_incident(processed)


def test_stale_d2_without_matching_processed_destination_fails_closed(harness) -> None:
    h = harness([])
    h.work.create(
        RuntimeWorkRecord(
            work_id=runtime_work_id(RuntimeWorkKind.AUTO_ASSIGN, "EVT-GHOST", "INC-GHOST"),
            work_kind=RuntimeWorkKind.AUTO_ASSIGN,
            event_id="EVT-GHOST",
            incident_id="INC-GHOST",
            stage="OUTSTANDING",
            next_action="AUTO_ASSIGN",
            workflow_operation_id=automatic_assignment_operation_id("INC-GHOST", ACTOR),
            attempt_count=0,
            retry_limit=3,
            status=RuntimeWorkStatus.OUTSTANDING,
            created_at=NOW,
            updated_at=NOW,
            observed_at=NOW,
        )
    )

    with pytest.raises(AutoAssignIntegrityError, match="no matching Processed"):
        h.assignment.recover_all()


def test_repeated_recovery_of_two_incidents_advances_round_robin_exactly_once_each(
    harness,
) -> None:
    events = [_event("EVT-A", "203.0.113.8"), _event("EVT-B", "203.0.113.9")]
    h = harness(events)
    first = h.create_processed_open("EVT-A")
    second = h.create_processed_open("EVT-B")

    h.assignment.recover_all()
    h.assignment.recover_all()

    assert h.incidents.get_incident(first.incident_id).assignee == "engineer-a"
    assert h.incidents.get_incident(second.incident_id).assignee == "engineer-b"
    assert h.incidents.get_assignment_state(POLICY.policy_id, POLICY.policy_version)[-1] == 0
    assert len(h.incidents.list_workflow_audit(first.incident_id)) == 1
    assert len(h.incidents.list_workflow_audit(second.incident_id)) == 1
