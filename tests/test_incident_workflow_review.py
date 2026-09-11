from dataclasses import replace
from datetime import timedelta

import pytest

from src.incident_management import ReviewAttemptPayload, SopFollowed, SqliteIncidentStore, WorkflowAction, WorkflowDomainError, WorkflowErrorCode, WorkflowMutationRequest
from test_incident_workflow_resolution import _bootstrap, _resolution
from test_incident_workflow_assignment import POLICY
from test_incident_sqlite_store import NOW


def _review(operation, revision, approved, recovered, *, minute=4, actor="Supervisor"):
    return WorkflowMutationRequest(operation, "INC-1", actor, NOW + timedelta(minutes=minute), WorkflowAction.REVIEW_ATTEMPT,
        review=ReviewAttemptPayload(revision, approved, "review note", recovered, "recovery note"))


@pytest.mark.parametrize("approved,recovered,closes", ((False, False, False), (True, False, False), (False, True, False), (True, True, True)))
def test_review_gates_are_same_attempt_and_negative_review_is_success(tmp_path, approved, recovered, closes):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    manager.submit_resolution(_resolution("RES-1", minute=3))
    result = manager.review_incident(_review("REV-1", 1, approved, recovered))
    incident = store.get_incident("INC-1")
    assert result.resulting_status is (incident.status)
    assert (incident.status.value == "CLOSED") is closes
    assert (incident.closed_at is not None) is closes
    assert len(store.list_review_attempts("INC-1")) == 1
    assert len(store.list_workflow_audit("INC-1")) == 4
    assert store.get_workflow_operation_result("REV-1") == result
    assert [entry.workflow_operation_id for entry in store.list_workflow_audit("INC-1")].count("REV-1") == 1


def test_stale_revision_and_cross_attempt_gates_cannot_close(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    manager.submit_resolution(_resolution("RES-1", minute=3))
    manager.review_incident(_review("REV-NEG", 1, True, False, minute=4))
    manager.submit_resolution(_resolution("RES-2", minute=5))
    with pytest.raises(WorkflowDomainError) as raised:
        manager.review_incident(_review("REV-STALE", 1, True, True, minute=6))
    assert raised.value.code is WorkflowErrorCode.STALE_RESOLUTION_REVISION
    assert store.get_incident("INC-1").status.value == "AWAITING_REVIEW"


def test_rca_failure_and_sop_deviation_do_not_block_legal_closure(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    current = store.get_incident("INC-1")
    with store._transaction() as tx:
        tx._replace_incident_state(replace(current, rca_status="FAILED"))
    manager.submit_resolution(_resolution("RES-SOP-NO", minute=3, sop=SopFollowed.NO, deviation="emergency exception"))
    manager.review_incident(_review("REV-CLOSE", 1, True, True))
    closed = store.get_incident("INC-1")
    assert (closed.status.value, closed.rca_status) == ("CLOSED", "FAILED")
    with pytest.raises(WorkflowDomainError) as raised:
        manager.submit_resolution(_resolution("RES-AFTER-CLOSE", minute=5))
    assert raised.value.code is WorkflowErrorCode.CLOSED_INCIDENT_MUTATION_FORBIDDEN


def test_reviewer_mismatch_failed_review_resubmission_and_closed_terminal_matrix(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    manager.submit_resolution(_resolution("RES-1", minute=3))
    with pytest.raises(WorkflowDomainError) as raised:
        manager.review_incident(_review("REV-BAD-ACTOR", 1, False, False, minute=4, actor="other"))
    assert raised.value.code is WorkflowErrorCode.WORKFLOW_ACTOR_MISMATCH
    manager.review_incident(_review("REV-NEG", 1, False, False, minute=4))
    manager.submit_resolution(_resolution("RES-2", minute=5))
    assert [item.revision for item in store.list_resolution_submissions("INC-1")] == [1, 2]
    manager.review_incident(_review("REV-CLOSE", 2, True, True, minute=6))
    closed = store.get_incident("INC-1")
    commands = (
        (manager.auto_assign_incident, WorkflowMutationRequest("CLOSED-AUTO", "INC-1", "operator", NOW + timedelta(minutes=7), WorkflowAction.AUTO_ASSIGN), POLICY),
        (manager.assign_incident, WorkflowMutationRequest("CLOSED-MANUAL", "INC-1", "operator", NOW + timedelta(minutes=7), WorkflowAction.MANUAL_ASSIGN, "Engineer A"), POLICY),
        (manager.reassign_incident, WorkflowMutationRequest("CLOSED-REASSIGN", "INC-1", "operator", NOW + timedelta(minutes=7), WorkflowAction.REASSIGN, "Engineer A"), POLICY),
        (manager.start_work, WorkflowMutationRequest("CLOSED-START", "INC-1", "Engineer A", NOW + timedelta(minutes=7), WorkflowAction.START_WORK), None),
        (manager.submit_resolution, _resolution("CLOSED-RES", minute=7), None),
        (manager.review_incident, _review("CLOSED-REVIEW", 2, True, True, minute=7), None),
    )
    for command, request, policy in commands:
        with pytest.raises(WorkflowDomainError) as raised:
            command(request) if policy is None else command(request, policy)
        assert raised.value.code is WorkflowErrorCode.CLOSED_INCIDENT_MUTATION_FORBIDDEN
    assert not hasattr(manager, "close_incident") and not hasattr(manager, "reopen_incident")
    assert store.get_incident("INC-1") == closed
