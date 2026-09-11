from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

import src.incident_management.contracts as contracts
from src.alert_correlation.state import RetryDisposition
from src.incident_management import (
    AssignmentPolicyConfig,
    IncidentStatus,
    ResolutionSubmission,
    ResolutionSubmissionPayload,
    ReviewAttempt,
    ReviewAttemptPayload,
    SopFollowed,
    WorkflowAction,
    WorkflowAuditEffect,
    WorkflowAuditEntry,
    WorkflowDomainError,
    WorkflowErrorCode,
    WorkflowMutationRequest,
    WorkflowOperationReceipt,
    WorkflowOperationResult,
    WorkflowReceiptSemanticIdentity,
    WORKFLOW_ERROR_DISPOSITIONS,
)


NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def _assert_error(code, factory, *args, **kwargs):
    with pytest.raises(WorkflowDomainError) as raised:
        factory(*args, **kwargs)
    assert raised.value.code is code


def _resolution(**changes):
    values = dict(actual_action="Restarted affected service", resolution_note="Service recovered", sop_followed=SopFollowed.YES)
    values.update(changes)
    return ResolutionSubmissionPayload(**values)


def _review(**changes):
    values = dict(target_resolution_revision=1, review_approved=True, review_note="Approved", recovery_verified=True, recovery_note="Verified")
    values.update(changes)
    return ReviewAttemptPayload(**values)


def _request(action=WorkflowAction.START_WORK, **changes):
    values = dict(workflow_operation_id="WOP-1", incident_id="INC-1", actor="Engineer A", now=NOW, action=action)
    if action in {WorkflowAction.MANUAL_ASSIGN, WorkflowAction.REASSIGN}:
        values["target_assignee"] = "Engineer B"
    if action is WorkflowAction.SUBMIT_RESOLUTION:
        values["resolution"] = _resolution()
    if action is WorkflowAction.REVIEW_ATTEMPT:
        values["review"] = _review()
    values.update(changes)
    return WorkflowMutationRequest(**values)


def test_workflow_action_and_sop_closed_sets_have_exact_members():
    assert [item.name for item in WorkflowAction] == [
        "AUTO_ASSIGN", "MANUAL_ASSIGN", "REASSIGN", "START_WORK", "SUBMIT_RESOLUTION", "REVIEW_ATTEMPT",
    ]
    assert "CLOSE" not in WorkflowAction.__members__
    assert [item.name for item in SopFollowed] == ["YES", "NO", "NOT_APPLICABLE"]


def test_assignment_policy_is_immutable_ordered_unique_bootstrap_configuration():
    policy = AssignmentPolicyConfig("POC-ROUND-ROBIN", "1.0", ["Engineer A", "Engineer B"], "Supervisor")
    assert policy.engineers == ("Engineer A", "Engineer B")
    assert {field.name for field in fields(policy)} == {"policy_id", "policy_version", "engineers", "default_reviewer"}
    for values in (("", "1.0", ("A",), "R"), ("P", "", ("A",), "R"), ("P", "1", (), "R"), ("P", "1", ("A", "A"), "R"), ("P", "1", "A", "R"), ("P", "1", ("A",), "")):
        _assert_error(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, AssignmentPolicyConfig, *values)


@pytest.mark.parametrize("payload", [
    dict(actual_action="", resolution_note="note", sop_followed=SopFollowed.YES),
    dict(actual_action="action", resolution_note="", sop_followed=SopFollowed.YES),
    dict(actual_action="action", resolution_note="note", sop_followed="YES"),
    dict(actual_action="action", resolution_note="note", sop_followed=SopFollowed.NO),
    dict(actual_action="action", resolution_note="note", sop_followed=SopFollowed.NO, deviation_reason=""),
])
def test_resolution_payload_required_evidence_and_sop_deviation(payload):
    _assert_error(WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE, ResolutionSubmissionPayload, **payload)
    no_sop = _resolution(sop_followed=SopFollowed.NO, deviation_reason="Emergency mitigation")
    assert no_sop.deviation_reason == "Emergency mitigation"


def test_resolution_logical_record_has_generated_identity_revision_and_submitter():
    record = ResolutionSubmission("RES-1", "INC-1", 1, "Restarted", "Recovered", SopFollowed.NOT_APPLICABLE, None, None, "Engineer A", NOW)
    assert record.submitted_at == NOW
    _assert_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, replace, record, revision=0)


def test_review_requires_exact_positive_target_and_boolean_facts_but_allows_negative_business_decisions():
    negative = _review(review_approved=False, recovery_verified=False, review_note=None, recovery_note=None)
    assert negative.review_approved is False and negative.recovery_verified is False
    _assert_error(WorkflowErrorCode.INVALID_REVIEW_ATTEMPT, ReviewAttemptPayload, 0, True, None, True, None)
    _assert_error(WorkflowErrorCode.INVALID_REVIEW_ATTEMPT, ReviewAttemptPayload, 1, 1, None, True, None)
    attempt = ReviewAttempt("REV-1", "INC-1", 1, "Supervisor", False, None, False, None, NOW)
    assert attempt.reviewer == "Supervisor"


def test_workflow_request_enforces_action_specific_payloads_and_authoritative_time():
    assert _request(WorkflowAction.AUTO_ASSIGN).target_assignee is None
    assert _request(WorkflowAction.MANUAL_ASSIGN).target_assignee == "Engineer B"
    assert _request(WorkflowAction.SUBMIT_RESOLUTION).resolution is not None
    assert _request(WorkflowAction.REVIEW_ATTEMPT).review.target_resolution_revision == 1
    _assert_error(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, _request, WorkflowAction.START_WORK, now=NOW.replace(tzinfo=None))
    _assert_error(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, _request, WorkflowAction.AUTO_ASSIGN, target_assignee="Engineer B")
    _assert_error(WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE, _request, WorkflowAction.SUBMIT_RESOLUTION, resolution=None)
    _assert_error(WorkflowErrorCode.INVALID_REVIEW_ATTEMPT, _request, WorkflowAction.REVIEW_ATTEMPT, review=None)
    direct_identity = WorkflowReceiptSemanticIdentity
    _assert_error(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, direct_identity, WorkflowAction.AUTO_ASSIGN, "", "Engineer A")
    _assert_error(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, direct_identity, WorkflowAction.AUTO_ASSIGN, "INC-1", "")
    _assert_error(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, direct_identity, "AUTO_ASSIGN", "INC-1", "Engineer A")
    _assert_error(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, direct_identity, WorkflowAction.AUTO_ASSIGN, "INC-1", "Engineer A", "Engineer B")
    _assert_error(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, direct_identity, WorkflowAction.START_WORK, "INC-1", "Engineer A", resolution=_resolution())
    _assert_error(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, direct_identity, WorkflowAction.MANUAL_ASSIGN, "INC-1", "Engineer A")
    _assert_error(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, direct_identity, WorkflowAction.REASSIGN, "INC-1", "Engineer A")
    _assert_error(WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE, direct_identity, WorkflowAction.SUBMIT_RESOLUTION, "INC-1", "Engineer A")
    _assert_error(WorkflowErrorCode.INVALID_REVIEW_ATTEMPT, direct_identity, WorkflowAction.REVIEW_ATTEMPT, "INC-1", "Engineer A")


@pytest.mark.parametrize("action", list(WorkflowAction))
def test_replay_identity_is_canonical_action_specific_and_excludes_authoritative_now(action):
    first = _request(action)
    retry = _request(action, now=NOW + timedelta(days=10))
    identity = WorkflowReceiptSemanticIdentity.from_request(first)
    assert identity == WorkflowReceiptSemanticIdentity.from_request(retry)
    assert "now" not in {field.name for field in fields(identity)}
    if action in {WorkflowAction.MANUAL_ASSIGN, WorkflowAction.REASSIGN}:
        assert identity != WorkflowReceiptSemanticIdentity.from_request(_request(action, target_assignee="Engineer C"))
    if action is WorkflowAction.SUBMIT_RESOLUTION:
        assert identity != WorkflowReceiptSemanticIdentity.from_request(_request(action, resolution=_resolution(resolution_note="Changed")))
    if action is WorkflowAction.REVIEW_ATTEMPT:
        assert identity != WorkflowReceiptSemanticIdentity.from_request(_request(action, review=_review(target_resolution_revision=2)))
    if action in {WorkflowAction.AUTO_ASSIGN, WorkflowAction.START_WORK}:
        direct_first = WorkflowReceiptSemanticIdentity(action, "INC-1", "Engineer A")
        direct_repeat = WorkflowReceiptSemanticIdentity(action, "INC-1", "Engineer A")
        direct_other_actor = WorkflowReceiptSemanticIdentity(action, "INC-1", "Engineer B")
        assert direct_first == direct_repeat
        assert direct_first != direct_other_actor


def test_workflow_receipt_result_and_audit_are_separate_from_correlation_contracts():
    request = _request(WorkflowAction.SUBMIT_RESOLUTION)
    result = WorkflowOperationResult("WOP-1", "INC-1", WorkflowAction.SUBMIT_RESOLUTION, contracts.WorkflowCompletion.SUCCEEDED, IncidentStatus.AWAITING_REVIEW, "RES-1", NOW)
    receipt = WorkflowOperationReceipt(result, WorkflowReceiptSemanticIdentity.from_request(request))
    assert receipt.result.result_reference == "RES-1"
    audit = WorkflowAuditEntry("WAUD-1", "WOP-1", "INC-1", "Engineer A", WorkflowAction.SUBMIT_RESOLUTION, NOW, IncidentStatus.IN_PROGRESS, IncidentStatus.AWAITING_REVIEW, (WorkflowAuditEffect.STATUS_CHANGED, WorkflowAuditEffect.RESOLUTION_SUBMITTED), "RES-1")
    assert audit.effects[-1] is WorkflowAuditEffect.RESOLUTION_SUBMITTED
    _assert_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, WorkflowOperationReceipt, replace(result, action=WorkflowAction.START_WORK), receipt.immutable_workflow_identity)


def test_workflow_error_vocabulary_and_dispositions_are_exact_and_closed():
    expected = {
        "INVALID_WORKFLOW_MUTATION", "INCIDENT_NOT_FOUND", "INVALID_LIFECYCLE_TRANSITION", "INVALID_ASSIGNMENT_TARGET",
        "ASSIGNMENT_NOT_ALLOWED", "WORKFLOW_ACTOR_MISMATCH", "INVALID_RESOLUTION_EVIDENCE", "RESOLUTION_REVISION_CONFLICT",
        "INVALID_REVIEW_ATTEMPT", "STALE_RESOLUTION_REVISION", "CLOSED_INCIDENT_MUTATION_FORBIDDEN", "WORKFLOW_RECEIPT_CONFLICT",
        "WORKFLOW_TIME_REGRESSION", "MALFORMED_WORKFLOW_STATE", "UNSUPPORTED_INCIDENT_STORE_VERSION",
        "INCIDENT_WORKFLOW_INTEGRITY_FAILURE", "TRANSIENT_INCIDENT_STORE_FAILURE",
    }
    assert {code.name for code in WorkflowErrorCode} == expected
    assert "REVIEW_GATE_NOT_SATISFIED" not in WorkflowErrorCode.__members__
    assert isinstance(WORKFLOW_ERROR_DISPOSITIONS, MappingProxyType)
    assert set(WORKFLOW_ERROR_DISPOSITIONS) == set(WorkflowErrorCode)
    assert WORKFLOW_ERROR_DISPOSITIONS[WorkflowErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE] is RetryDisposition.RETRYABLE
    assert WORKFLOW_ERROR_DISPOSITIONS[WorkflowErrorCode.WORKFLOW_RECEIPT_CONFLICT] is RetryDisposition.REPAIR_REQUIRED
    assert WORKFLOW_ERROR_DISPOSITIONS[WorkflowErrorCode.WORKFLOW_TIME_REGRESSION] is RetryDisposition.NON_RETRYABLE
    # Workflow requests carry their own payloads; they never accept a 007 intent.
    assert {field.name for field in fields(WorkflowMutationRequest)} == {
        "workflow_operation_id", "incident_id", "actor", "now", "action",
        "target_assignee", "resolution", "review",
    }
