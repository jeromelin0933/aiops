from datetime import datetime, timezone
import sqlite3

import pytest

from src.alert_correlation import AnchorStrength, CorrelationFamily, DecisionReasonCode, NormalizedFingerprint
from src.incident_management import (
    RCA_INITIAL_STATUS, IncidentAuditAction, IncidentAuditEffect, IncidentAuditEntry,
    IncidentCorrelationContext, IncidentDomainError, IncidentRecord, IncidentSeverity,
    IncidentStatus, ResolutionSubmission, ResolutionSubmissionPayload, ReviewAttempt,
    SopFollowed, SqliteIncidentStore, WorkflowAction, WorkflowAuditEffect, WorkflowAuditEntry,
    WorkflowCompletion, WorkflowDomainError, WorkflowErrorCode, WorkflowOperationReceipt,
    WorkflowOperationResult, WorkflowReceiptSemanticIdentity,
)


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
FINGERPRINT = NormalizedFingerprint.from_mapping("brute_force_detected", {"source_ip": "192.0.2.10"})


def _record() -> IncidentRecord:
    audit = IncidentAuditEntry(
        operation_id="CORR-1", event_id="EVT-1", incident_id="INC-1", policy_id="POLICY-1",
        policy_version="1", reason_code=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
        action=IncidentAuditAction.INCIDENT_CREATED, effects=(IncidentAuditEffect.EVENT_ATTACHED,), occurred_at=NOW,
    )
    return IncidentRecord(
        incident_id="INC-1", event_ids=("EVT-1",), anchor_event_id="EVT-1", status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL, created_at=NOW, updated_at=NOW, last_correlated_at=NOW,
        closed_at=None, assignee=None, reviewer=None,
        correlation_context=IncidentCorrelationContext(
            correlation_family=CorrelationFamily.ATTACK_SOURCE, anchor_strength=AnchorStrength.STRONG,
            anchor_event_id="EVT-1", anchor_event_type="brute_force_detected", normalized_fingerprint=FINGERPRINT,
            anchor_policy_id="POLICY-1", anchor_policy_version="1",
        ), audit_trail=(audit,), rca_status=RCA_INITIAL_STATUS, rca_ref=None, external_refs=("JIRA-1",),
    )


def _seed_incident(store: SqliteIncidentStore) -> IncidentRecord:
    record = _record()
    # This is intentionally incomplete SPEC-008 evidence: these tests exercise
    # only the Phase-2 workflow tables and never invoke whole-store integrity.
    with store._transaction() as tx:
        tx._insert_incident_state(record)
    return record


def _workflow_receipt() -> WorkflowOperationReceipt:
    payload = ResolutionSubmissionPayload("blocked IP", "host recovered", SopFollowed.YES)
    identity = WorkflowReceiptSemanticIdentity(WorkflowAction.SUBMIT_RESOLUTION, "INC-1", "eng-a", None, payload)
    result = WorkflowOperationResult("WF-1", "INC-1", WorkflowAction.SUBMIT_RESOLUTION,
        WorkflowCompletion.SUCCEEDED, IncidentStatus.AWAITING_REVIEW, "RES-1", NOW)
    return WorkflowOperationReceipt(result, identity)


def _seed_workflow_state(store: SqliteIncidentStore) -> tuple[WorkflowOperationReceipt, tuple[ResolutionSubmission, ...], ReviewAttempt, WorkflowAuditEntry]:
    receipt = _workflow_receipt()
    first = ResolutionSubmission("RES-1", "INC-1", 1, "blocked IP", "host recovered", SopFollowed.YES, None, None, "eng-a", NOW)
    second = ResolutionSubmission("RES-2", "INC-1", 2, "reset account", "credentials rotated", SopFollowed.NO, "exception", "emergency containment", "eng-a", NOW)
    review = ReviewAttempt("REV-1", "INC-1", 2, "review-a", False, "need more evidence", False, None, NOW)
    audit = WorkflowAuditEntry("WFA-1", "WF-1", "INC-1", "eng-a", WorkflowAction.SUBMIT_RESOLUTION, NOW,
        IncidentStatus.IN_PROGRESS, IncidentStatus.AWAITING_REVIEW,
        (WorkflowAuditEffect.STATUS_CHANGED, WorkflowAuditEffect.RESOLUTION_SUBMITTED), "RES-1")
    with store._transaction() as tx:
        tx._upsert_assignment_state("RR", "1", ("eng-a", "eng-b"), "review-a", 1)
        tx._insert_resolution_submission(first)
        tx._insert_resolution_submission(second)
        tx._insert_review_attempt(review)
        tx._insert_workflow_receipt(receipt)
        tx._insert_workflow_audit(audit)
    return receipt, (first, second), review, audit


def test_workflow_primitives_are_durable_canonical_and_read_only(tmp_path):
    database = tmp_path / "incident.db"
    store = SqliteIncidentStore(str(database))
    _seed_incident(store)
    receipt, resolutions, review, audit = _seed_workflow_state(store)
    store.close()

    with SqliteIncidentStore(str(database)) as reopened:
        assert reopened.get_workflow_operation_result("WF-1") == receipt.result
        assert reopened.get_assignment_state("RR", "1") == (("eng-a", "eng-b"), "review-a", 1)
        assert reopened.get_latest_resolution_submission("INC-1") == resolutions[-1]
        assert reopened.list_resolution_submissions("INC-1") == resolutions
        assert reopened.list_review_attempts("INC-1") == (review,)
        assert reopened.list_workflow_audit("INC-1") == (audit,)

    with sqlite3.connect(database) as connection:
        identity = connection.execute("SELECT immutable_workflow_identity FROM incident_workflow_operation_receipts").fetchone()[0]
    assert identity == ('{"action":"SUBMIT_RESOLUTION","actor":"eng-a","incident_id":"INC-1",'
                        '"resolution":{"actual_action":"blocked IP","additional_note":null,'
                        '"deviation_reason":null,"resolution_note":"host recovered","sop_followed":"YES"},'
                        '"review":null,"target_assignee":null}')


def test_resolution_history_malformed_state_fails_closed_even_for_latest_lookup(tmp_path):
    database = tmp_path / "incident.db"
    store = SqliteIncidentStore(str(database))
    _seed_incident(store)
    _seed_workflow_state(store)
    store.close()
    # Controlled corruption models an on-disk record from a faulty binary; it
    # is not exposed by the store's mutation surface.
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM incident_review_attempts")
        connection.execute("UPDATE incident_resolution_submissions SET revision = 3 WHERE resolution_submission_id = 'RES-2'")

    with pytest.raises(WorkflowDomainError) as raised:
        SqliteIncidentStore(str(database)).get_latest_resolution_submission("INC-1")
    assert raised.value.code is WorkflowErrorCode.MALFORMED_WORKFLOW_STATE


def test_workflow_database_constraints_reject_reused_revision_and_orphan_review(tmp_path):
    store = SqliteIncidentStore(str(tmp_path / "incident.db"))
    _seed_incident(store)
    _seed_workflow_state(store)
    duplicate = ResolutionSubmission("RES-3", "INC-1", 2, "other", "other", SopFollowed.YES, None, None, "eng-a", NOW)
    orphan = ReviewAttempt("REV-2", "INC-1", 99, "review-a", True, None, True, None, NOW)

    with pytest.raises(IncidentDomainError):
        with store._transaction() as tx:
            tx._insert_resolution_submission(duplicate)
    with pytest.raises(IncidentDomainError):
        with store._transaction() as tx:
            tx._insert_review_attempt(orphan)
