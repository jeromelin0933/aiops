from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest

from src.incident_management import (
    AssignmentPolicyConfig, IncidentManager, IncidentStatus, SqliteIncidentStore,
    WorkflowAction, WorkflowDomainError, WorkflowErrorCode, WorkflowMutationRequest,
)
from _incident_store_testkit import seed_complete_incident
from test_incident_sqlite_store import NOW, _receipt, _record


POLICY = AssignmentPolicyConfig("RR-POC", "1", ("Engineer A", "Engineer B", "Engineer C"), "Supervisor")


def _seed(store, number: int):
    event, operation, incident = f"EVT-{number}", f"CORR-{number}", f"INC-{number}"
    record = _record(event, operation, incident)
    seed_complete_incident(store, record, (_receipt(event, operation, incident),))
    return record


def _request(number: int, action: WorkflowAction, *, actor="operator", target=None, minutes=1):
    return WorkflowMutationRequest(f"WF-{number}-{action.value}", f"INC-{number}", actor, NOW + timedelta(minutes=minutes), action, target)


def test_auto_assign_round_robin_and_restart_continuity(tmp_path):
    database = tmp_path / "incident.db"
    store = SqliteIncidentStore(str(database))
    for number in range(1, 5):
        _seed(store, number)
    manager = IncidentManager(store)
    for number, assignee in enumerate(("Engineer A", "Engineer B", "Engineer C", "Engineer A"), 1):
        result = manager.auto_assign_incident(_request(number, WorkflowAction.AUTO_ASSIGN), POLICY)
        assert result.selected_assignee == assignee
        assert result.bound_reviewer == "Supervisor"
        assert result.assignment_policy_id == "RR-POC"
        assert store.get_incident(f"INC-{number}").reviewer == "Supervisor"
    store.close()
    with SqliteIncidentStore(str(database)) as reopened:
        assert reopened.get_assignment_state("RR-POC", "1") == (POLICY.engineers, "Supervisor", 1)


def test_concurrent_auto_assignment_serializes_durable_cursor(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    for number in range(1, 4):
        _seed(store, number)
    manager = IncidentManager(store)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda n: manager.auto_assign_incident(_request(n, WorkflowAction.AUTO_ASSIGN), POLICY), range(1, 4)))
    assert {result.selected_assignee for result in results} == set(POLICY.engineers)
    assert store.get_assignment_state("RR-POC", "1")[-1] == 0


def test_failed_auto_does_not_change_cursor_or_incident(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    original = _seed(store, 1)
    manager = IncidentManager(store)
    stale = _request(1, WorkflowAction.AUTO_ASSIGN, minutes=-1)
    with pytest.raises(WorkflowDomainError) as raised:
        manager.auto_assign_incident(stale, POLICY)
    assert raised.value.code is WorkflowErrorCode.WORKFLOW_TIME_REGRESSION
    assert store.get_incident("INC-1") == original
    assert store.get_assignment_state("RR-POC", "1") is None
    assert store.get_workflow_operation_result(stale.workflow_operation_id) is None


def test_manual_and_reassign_do_not_advance_cursor_and_bind_reviewer(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    _seed(store, 1)
    manager = IncidentManager(store)
    manual = _request(1, WorkflowAction.MANUAL_ASSIGN, target="Engineer C")
    result = manager.assign_incident(manual, POLICY)
    assert result.selection_mode.value == "MANUAL"
    assert store.get_assignment_state("RR-POC", "1") is None
    assigned = store.get_incident("INC-1")
    assert (assigned.assignee, assigned.reviewer, assigned.status) == ("Engineer C", "Supervisor", IncidentStatus.ASSIGNED)
    manager.reassign_incident(_request(1, WorkflowAction.REASSIGN, target="Engineer B", minutes=2), POLICY)
    reassigned = store.get_incident("INC-1")
    assert (reassigned.assignee, reassigned.reviewer, reassigned.status) == ("Engineer B", "Supervisor", IncidentStatus.ASSIGNED)
    assert store.get_assignment_state("RR-POC", "1") is None
    assert store.list_workflow_audit("INC-1")[0].assignment_policy_id is None


def test_start_work_actor_time_and_correlation_fields_are_guarded(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    original = _seed(store, 1)
    manager = IncidentManager(store)
    manager.assign_incident(_request(1, WorkflowAction.MANUAL_ASSIGN, target="Engineer A"), POLICY)
    with pytest.raises(WorkflowDomainError) as raised:
        manager.start_work(_request(1, WorkflowAction.START_WORK, actor="other", minutes=2))
    assert raised.value.code is WorkflowErrorCode.WORKFLOW_ACTOR_MISMATCH
    manager.start_work(_request(1, WorkflowAction.START_WORK, actor="Engineer A", minutes=2))
    current = store.get_incident("INC-1")
    assert current.status is IncidentStatus.IN_PROGRESS
    assert (current.event_ids, current.anchor_event_id, current.severity, current.last_correlated_at,
            current.correlation_context, current.rca_status, current.rca_ref, current.external_refs) == (
            original.event_ids, original.anchor_event_id, original.severity, original.last_correlated_at,
            original.correlation_context, original.rca_status, original.rca_ref, original.external_refs)


@pytest.mark.parametrize("status", (IncidentStatus.AWAITING_REVIEW, IncidentStatus.CLOSED))
def test_reassign_rejects_awaiting_review_and_closed(tmp_path, status):
    store = SqliteIncidentStore(str(tmp_path / f"{status.value}.db"))
    _seed(store, 1)
    manager = IncidentManager(store)
    manager.assign_incident(_request(1, WorkflowAction.MANUAL_ASSIGN, target="Engineer A"), POLICY)
    current = store.get_incident("INC-1")
    with store._transaction() as tx:
        tx._replace_incident_state(replace(current, status=status, closed_at=current.updated_at if status is IncidentStatus.CLOSED else None))
    with pytest.raises(WorkflowDomainError) as raised:
        manager.reassign_incident(_request(1, WorkflowAction.REASSIGN, target="Engineer B", minutes=2), POLICY)
    assert raised.value.code is (WorkflowErrorCode.CLOSED_INCIDENT_MUTATION_FORBIDDEN if status is IncidentStatus.CLOSED else WorkflowErrorCode.ASSIGNMENT_NOT_ALLOWED)


def test_strict_lifecycle_rejects_skip_backward_and_preserves_in_progress_reassign(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    _seed(store, 1)
    manager = IncidentManager(store)
    with pytest.raises(WorkflowDomainError) as raised:
        manager.start_work(_request(1, WorkflowAction.START_WORK, actor="Engineer A"))
    assert raised.value.code is WorkflowErrorCode.INVALID_LIFECYCLE_TRANSITION
    manager.assign_incident(_request(1, WorkflowAction.MANUAL_ASSIGN, target="Engineer A"), POLICY)
    manager.start_work(_request(1, WorkflowAction.START_WORK, actor="Engineer A", minutes=2))
    manager.reassign_incident(_request(1, WorkflowAction.REASSIGN, target="Engineer B", minutes=3), POLICY)
    assert store.get_incident("INC-1").status is IncidentStatus.IN_PROGRESS
    with pytest.raises(WorkflowDomainError) as raised:
        manager.assign_incident(WorkflowMutationRequest("WF-BACKWARD", "INC-1", "operator", NOW + timedelta(minutes=4), WorkflowAction.MANUAL_ASSIGN, "Engineer A"), POLICY)
    assert raised.value.code is WorkflowErrorCode.ASSIGNMENT_NOT_ALLOWED
