import pytest
from concurrent.futures import ThreadPoolExecutor

from src.incident_management import AssignmentPolicyConfig, IncidentManager, IncidentStatus, IncidentTimelineSource, SqliteIncidentStore, WorkflowAction, WorkflowDomainError, WorkflowErrorCode
from test_incident_workflow_assignment import POLICY, _request, _seed
from test_incident_workflow_resolution import _bootstrap, _resolution
from test_incident_manager import _attach_request


def test_auto_assign_replay_x1_x10_x100_does_not_resolve_new_policy_or_cursor(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    _seed(store, 1)
    manager = IncidentManager(store)
    request = _request(1, WorkflowAction.AUTO_ASSIGN)
    original = manager.auto_assign_incident(request, POLICY)
    changed_policy = AssignmentPolicyConfig("RR-POC", "1", ("different",), "different-reviewer")
    for repeats in (1, 10, 100):
        for _ in range(repeats):
            assert manager.auto_assign_incident(request, changed_policy) == original
    assert store.get_assignment_state("RR-POC", "1") == (POLICY.engineers, "Supervisor", 1)
    assert len(store.list_workflow_audit("INC-1")) == 1


def test_contradictory_replay_fails_closed_without_extra_audit(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    _seed(store, 1)
    manager = IncidentManager(store)
    request = _request(1, WorkflowAction.MANUAL_ASSIGN, target="Engineer A")
    manager.assign_incident(request, POLICY)
    contradictory = _request(1, WorkflowAction.MANUAL_ASSIGN, target="Engineer B")
    with pytest.raises(WorkflowDomainError) as raised:
        manager.assign_incident(contradictory, POLICY)
    assert raised.value.code is WorkflowErrorCode.WORKFLOW_RECEIPT_CONFLICT
    assert len(store.list_workflow_audit("INC-1")) == 1


def test_resolution_replay_x1_x10_x100_is_exactly_once(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    request = _resolution("RES-REPLAY", minute=3)
    original = manager.submit_resolution(request)
    for repeats in (1, 10, 100):
        for _ in range(repeats):
            assert manager.submit_resolution(request) == original
    assert len(store.list_resolution_submissions("INC-1")) == 1
    assert len(store.list_workflow_audit("INC-1")) == 3


def test_restart_replay_returns_original_result_without_new_revision(tmp_path):
    database = tmp_path / "incident.db"
    store = SqliteIncidentStore(str(database))
    manager = _bootstrap(store)
    request = _resolution("RES-RESTART", minute=3)
    original = manager.submit_resolution(request)
    store.close()
    reopened = SqliteIncidentStore(str(database))
    assert IncidentManager(reopened).submit_resolution(request) == original
    assert len(reopened.list_resolution_submissions("INC-1")) == 1


def test_concurrent_resolution_allocation_is_serialized(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    first = _resolution("RES-CONCURRENT-1", minute=3)
    second = _resolution("RES-CONCURRENT-2", minute=3)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(manager.submit_resolution, (first, second)))
    assert {result.resulting_status for result in results} == {IncidentStatus.AWAITING_REVIEW}
    assert [item.revision for item in store.list_resolution_submissions("INC-1")] == [1, 2]


def test_unified_timeline_tie_is_source_aware_and_malformed_rows_fail_closed(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    _seed(store, 1)
    manager = IncidentManager(store)
    manager.assign_incident(_request(1, WorkflowAction.MANUAL_ASSIGN, target="Engineer A", minutes=0), POLICY)
    timeline = store.list_unified_incident_timeline("INC-1")
    assert [entry.source for entry in timeline[:2]] == [IncidentTimelineSource.CORRELATION, IncidentTimelineSource.WORKFLOW]
    assert store.list_incidents_by_workflow_status(IncidentStatus.ASSIGNED)[0].incident_id == "INC-1"
    with store._transaction() as tx:
        tx._connection.execute("UPDATE incident_workflow_audit SET effects = '[]' WHERE incident_id = 'INC-1'")
    with pytest.raises(WorkflowDomainError) as raised:
        store.list_unified_incident_timeline("INC-1")
    assert raised.value.code is WorkflowErrorCode.MALFORMED_WORKFLOW_STATE


def test_concurrent_attach_vs_submit_is_fresh_read_safe(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    attach = _attach_request("EVT-ATTACH-RACE", operation_id="CORR-ATTACH-RACE", target="INC-1")
    submit = _resolution("RES-ATTACH-RACE", minute=3)
    def invoke(function, request):
        try:
            return function(request)
        except Exception as exc:  # The losing correlation write is expected to reject typedly.
            return exc
    with ThreadPoolExecutor(max_workers=2) as executor:
        attached, submitted = list(executor.map(lambda pair: invoke(*pair), ((manager.apply_correlation_mutation, attach), (manager.submit_resolution, submit))))
    assert not (isinstance(attached, Exception) and isinstance(submitted, Exception))
    incident = store.get_incident("INC-1")
    assert incident.status is IncidentStatus.AWAITING_REVIEW
    if isinstance(attached, Exception):
        assert "correlation-open" in str(attached)
    else:
        assert "EVT-ATTACH-RACE" in incident.event_ids


def test_concurrent_review_n_vs_resolution_n_plus_one_never_stale_closes(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    manager.submit_resolution(_resolution("RES-N", minute=3))
    from test_incident_workflow_review import _review
    review = _review("REV-N", 1, True, True, minute=4)
    resubmit = _resolution("RES-N-PLUS-ONE", minute=4)
    def invoke(function, request):
        try:
            return function(request)
        except Exception as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as executor:
        reviewed, resubmitted = list(executor.map(lambda pair: invoke(*pair), ((manager.review_incident, review), (manager.submit_resolution, resubmit))))
    incident = store.get_incident("INC-1")
    if incident.status is IncidentStatus.CLOSED:
        assert isinstance(resubmitted, WorkflowDomainError)
        assert resubmitted.code is WorkflowErrorCode.CLOSED_INCIDENT_MUTATION_FORBIDDEN
    else:
        assert incident.status is IncidentStatus.AWAITING_REVIEW
        assert isinstance(reviewed, WorkflowDomainError)
        assert reviewed.code is WorkflowErrorCode.STALE_RESOLUTION_REVISION
        assert [item.revision for item in store.list_resolution_submissions("INC-1")] == [1, 2]


@pytest.mark.parametrize("action", tuple(WorkflowAction))
def test_every_workflow_action_has_exactly_once_equivalent_replay(tmp_path, action):
    store = SqliteIncidentStore(str(tmp_path / f"{action.value}.db"))
    if action is WorkflowAction.AUTO_ASSIGN:
        _seed(store, 1); manager = IncidentManager(store); request = _request(1, action)
        invoke = lambda: manager.auto_assign_incident(request, POLICY)
    elif action is WorkflowAction.MANUAL_ASSIGN:
        _seed(store, 1); manager = IncidentManager(store); request = _request(1, action, target="Engineer A")
        invoke = lambda: manager.assign_incident(request, POLICY)
    elif action is WorkflowAction.REASSIGN:
        _seed(store, 1); manager = IncidentManager(store)
        manager.assign_incident(_request(1, WorkflowAction.MANUAL_ASSIGN, target="Engineer A"), POLICY)
        request = _request(1, action, target="Engineer B", minutes=2); invoke = lambda: manager.reassign_incident(request, POLICY)
    elif action is WorkflowAction.START_WORK:
        _seed(store, 1); manager = IncidentManager(store)
        manager.assign_incident(_request(1, WorkflowAction.MANUAL_ASSIGN, target="Engineer A"), POLICY)
        request = _request(1, action, actor="Engineer A", minutes=2); invoke = lambda: manager.start_work(request)
    elif action is WorkflowAction.SUBMIT_RESOLUTION:
        manager = _bootstrap(store); request = _resolution("RES-MATRIX", minute=3); invoke = lambda: manager.submit_resolution(request)
    else:
        manager = _bootstrap(store); manager.submit_resolution(_resolution("RES-MATRIX", minute=3))
        from test_incident_workflow_review import _review
        request = _review("REV-MATRIX", 1, False, False, minute=4); invoke = lambda: manager.review_incident(request)
    original = invoke()
    for repeats in (1, 10, 100):
        for _ in range(repeats):
            assert invoke() == original
    assert len([entry for entry in store.list_workflow_audit("INC-1") if entry.workflow_operation_id == request.workflow_operation_id]) == 1


def test_workflow_status_projects_from_the_same_incident_authority(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    manager = _bootstrap(store)
    manager.submit_resolution(_resolution("RES-VIEW", minute=3))
    assert store.get_correlation_view("INC-1").status == IncidentStatus.AWAITING_REVIEW.value
    from test_incident_workflow_review import _review
    manager.review_incident(_review("REV-VIEW", 1, True, True, minute=4))
    assert store.get_correlation_view("INC-1").status == IncidentStatus.CLOSED.value
