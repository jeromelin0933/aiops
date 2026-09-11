from datetime import timedelta

import pytest

from src.incident_management import (
    IncidentManager, IncidentStatus, ResolutionSubmissionPayload, SopFollowed,
    SqliteIncidentStore, WorkflowAction, WorkflowDomainError, WorkflowErrorCode, WorkflowMutationRequest,
)
from test_incident_workflow_assignment import POLICY, _request, _seed
from test_incident_sqlite_store import NOW


def _bootstrap(store):
    _seed(store, 1)
    manager = IncidentManager(store)
    manager.assign_incident(_request(1, WorkflowAction.MANUAL_ASSIGN, target="Engineer A"), POLICY)
    manager.start_work(_request(1, WorkflowAction.START_WORK, actor="Engineer A", minutes=2))
    return manager


def _resolution(operation, *, minute, actor="Engineer A", sop=SopFollowed.YES, deviation=None):
    return WorkflowMutationRequest(operation, "INC-1", actor, NOW + timedelta(minutes=minute), WorkflowAction.SUBMIT_RESOLUTION,
        resolution=ResolutionSubmissionPayload("contain incident", "service is healthy", sop, deviation_reason=deviation))


def test_initial_and_pre_review_resubmission_are_append_only(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    first = manager.submit_resolution(_resolution("RES-OP-1", minute=3))
    second = manager.submit_resolution(_resolution("RES-OP-2", minute=4))
    history = store.list_resolution_submissions("INC-1")
    assert [item.revision for item in history] == [1, 2]
    assert first.resulting_status is second.resulting_status is IncidentStatus.AWAITING_REVIEW
    assert store.get_latest_resolution_submission("INC-1") == history[-1]
    assert store.get_incident("INC-1").assignee == "Engineer A"


def test_resolution_validation_actor_and_closed_terminal(tmp_path):
    with pytest.raises(WorkflowDomainError):
        ResolutionSubmissionPayload("action", "note", SopFollowed.NO)
    with pytest.raises(WorkflowDomainError):
        ResolutionSubmissionPayload("", "note", SopFollowed.YES)
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    with pytest.raises(WorkflowDomainError) as raised:
        manager.submit_resolution(_resolution("RES-BAD-ACTOR", minute=3, actor="other"))
    assert raised.value.code is WorkflowErrorCode.WORKFLOW_ACTOR_MISMATCH
