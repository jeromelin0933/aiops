from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.alert_correlation import (
    AnchorStrength,
    AnchorTransition,
    CorrelationDecision,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    NormalizedFingerprint,
)
from src.alert_correlation.state import (
    CorrelationMutationIntent,
    RetryDisposition,
    StateDomainValidationError,
)
from src.incident_management import (
    RCA_INITIAL_STATUS,
    IncidentAuditAction,
    IncidentAuditEffect,
    IncidentDomainError,
    IncidentErrorCode,
    IncidentManager,
    IncidentMutationRequest,
    IncidentSeverity,
    IncidentStatus,
    SqliteIncidentStore,
)
from src.incident_management.sqlite_store import _IncidentStoreTransaction

from _incident_store_testkit import execute_controlled_sql


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _fingerprint(event_type="brute_force_detected", source_ip="192.0.2.10"):
    return NormalizedFingerprint.from_mapping(
        event_type, {"source_ip": source_ip}
    )


def _event(
    event_id="EVT-1",
    *,
    event_type="brute_force_detected",
    detected_at="2026-09-09T11:59:00Z",
    severity="HIGH",
):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "detected_at": detected_at,
        "severity": severity,
    }


def _create_decision(strength=AnchorStrength.STRONG, *, fingerprint=None):
    if fingerprint is None and strength is AnchorStrength.STRONG:
        fingerprint = _fingerprint()
    return CorrelationDecision(
        decision_type=DecisionType.CREATE_NEW,
        policy_id="POLICY-CREATE",
        policy_version="1.0",
        correlation_family=CorrelationFamily.ATTACK_SOURCE,
        reason_code=(
            DecisionReasonCode.NO_COMPATIBLE_CANDIDATE
            if strength is AnchorStrength.STRONG
            else DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED
        ),
        normalized_fingerprint=fingerprint,
        anchor_strength=strength,
    )


def _attach_decision(
    target="INC-1",
    *,
    strength=AnchorStrength.STRONG,
    family=CorrelationFamily.ATTACK_SOURCE,
    fingerprint=None,
    transition=AnchorTransition.NONE,
):
    if fingerprint is None and strength is AnchorStrength.STRONG:
        fingerprint = _fingerprint()
    reason = (
        DecisionReasonCode.WEAK_TO_STRONG_PROMOTION
        if transition is AnchorTransition.WEAK_TO_STRONG
        else (
            DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH
            if fingerprint is not None
            else DecisionReasonCode.UNIQUE_COMPATIBLE_CANDIDATE
        )
    )
    return CorrelationDecision(
        decision_type=DecisionType.ATTACH_EXISTING,
        policy_id="POLICY-ATTACH",
        policy_version="1.0",
        correlation_family=family,
        reason_code=reason,
        target_incident_id=target,
        normalized_fingerprint=fingerprint,
        anchor_strength=strength,
        anchor_transition=transition,
    )


def _request(
    decision,
    event,
    *,
    operation_id,
    now=NOW,
):
    intent = CorrelationMutationIntent.from_decision(
        operation_id=operation_id,
        event_id=event["event_id"],
        decision=decision,
        created_at=NOW - timedelta(minutes=5),
    )
    return IncidentMutationRequest(intent, event, now)


def _create_request(
    event_id="EVT-1",
    *,
    operation_id="OP-CREATE",
    strength=AnchorStrength.STRONG,
    severity="HIGH",
    fingerprint=None,
    now=NOW,
):
    return _request(
        _create_decision(strength, fingerprint=fingerprint),
        _event(event_id, severity=severity),
        operation_id=operation_id,
        now=now,
    )


def _attach_request(
    event_id="EVT-2",
    *,
    operation_id="OP-ATTACH",
    target="INC-1",
    strength=AnchorStrength.STRONG,
    family=CorrelationFamily.ATTACK_SOURCE,
    fingerprint=None,
    transition=AnchorTransition.NONE,
    detected_at="2026-09-09T12:01:00Z",
    severity="HIGH",
    now=NOW + timedelta(minutes=2),
):
    return _request(
        _attach_decision(
            target,
            strength=strength,
            family=family,
            fingerprint=fingerprint,
            transition=transition,
        ),
        _event(event_id, detected_at=detected_at, severity=severity),
        operation_id=operation_id,
        now=now,
    )


def _manager(tmp_path, *, incident_id="INC-1"):
    store = SqliteIncidentStore(str(tmp_path / "incidents.db"))
    return store, IncidentManager(store, incident_id_factory=lambda: incident_id)


def _assert_error(code, callable_, *args, **kwargs):
    with pytest.raises(IncidentDomainError) as raised:
        callable_(*args, **kwargs)
    assert raised.value.code is code
    return raised.value


def test_strong_create_builds_open_incident_ownership_receipt_and_audit(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        result = manager.apply_correlation_mutation(_create_request())
        incident = store.get_incident(result.incident_id)

        assert result.incident_id == "INC-1"
        assert result.completed_at == NOW
        assert incident.event_ids == ("EVT-1",)
        assert incident.status is IncidentStatus.OPEN
        assert incident.severity is IncidentSeverity.HIGH
        assert incident.created_at == incident.updated_at == NOW
        assert incident.last_correlated_at == NOW - timedelta(minutes=1)
        assert incident.assignee is incident.reviewer is None
        assert incident.rca_status == RCA_INITIAL_STATUS == "PENDING"
        assert incident.rca_ref is None
        assert incident.correlation_context.anchor_strength is AnchorStrength.STRONG
        assert incident.anchor_event_id == "EVT-1"
        assert incident.correlation_context.anchor_event_type == "brute_force_detected"
        assert incident.correlation_context.normalized_fingerprint == _fingerprint()
        assert incident.correlation_context.promoted_from_weak is False
        assert incident.audit_trail[0].action is IncidentAuditAction.INCIDENT_CREATED
        assert incident.audit_trail[0].effects == (IncidentAuditEffect.EVENT_ATTACHED,)
        assert store.get_operation_result("OP-CREATE") == result
        store.validate_integrity()


def test_weak_standalone_create_has_null_anchor_but_retains_family_and_policy(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        result = manager.apply_correlation_mutation(
            _create_request(strength=AnchorStrength.WEAK, severity="LOW")
        )
        incident = store.get_incident(result.incident_id)
        context = incident.correlation_context

        assert incident.anchor_event_id is None
        assert context.anchor_strength is AnchorStrength.WEAK
        assert context.anchor_event_id is None
        assert context.anchor_event_type is None
        assert context.normalized_fingerprint is None
        assert context.correlation_family is CorrelationFamily.ATTACK_SOURCE
        assert (context.anchor_policy_id, context.anchor_policy_version) == (
            "POLICY-CREATE",
            "1.0",
        )


def test_create_rejects_event_fingerprint_type_conflict_atomically(tmp_path):
    store, manager = _manager(tmp_path)
    conflicting = _fingerprint("different_event_type")
    request = _create_request(fingerprint=conflicting)
    with store:
        error = _assert_error(
            IncidentErrorCode.INVALID_INCIDENT_MUTATION,
            manager.apply_correlation_mutation,
            request,
        )
        assert error.retry_disposition is RetryDisposition.NON_RETRYABLE
        assert store.incident_exists("INC-1") is False
        assert store.get_operation_result("OP-CREATE") is None


def test_valid_attach_mutates_exact_target_and_preserves_order_and_anchor(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request(severity="LOW"))
        original = store.get_incident("INC-1")
        result = manager.apply_correlation_mutation(
            _attach_request(severity="CRITICAL")
        )
        attached = store.get_incident("INC-1")

        assert result.incident_id == "INC-1"
        assert attached.event_ids == ("EVT-1", "EVT-2")
        assert attached.severity is IncidentSeverity.CRITICAL
        assert attached.last_correlated_at == NOW + timedelta(minutes=1)
        assert attached.updated_at == NOW + timedelta(minutes=2)
        assert attached.anchor_event_id == original.anchor_event_id
        assert attached.correlation_context == original.correlation_context
        assert attached.audit_trail[-1].action is IncidentAuditAction.CORRELATION_ATTACHED
        assert attached.audit_trail[-1].effects == (
            IncidentAuditEffect.EVENT_ATTACHED,
            IncidentAuditEffect.SEVERITY_ESCALATED,
        )
        assert store.get_operation_result("OP-ATTACH") == result
        store.validate_integrity()


def test_ordinary_weak_attach_preserves_null_weak_anchor(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(
            _create_request(strength=AnchorStrength.WEAK)
        )
        request = _attach_request(
            strength=AnchorStrength.WEAK,
            fingerprint=None,
        )
        manager.apply_correlation_mutation(request)
        incident = store.get_incident("INC-1")

        assert incident.event_ids == ("EVT-1", "EVT-2")
        assert incident.anchor_event_id is None
        assert incident.correlation_context.anchor_strength is AnchorStrength.WEAK
        assert incident.correlation_context.normalized_fingerprint is None


def test_weak_supporting_event_can_attach_to_strong_without_replacing_anchor(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        before = store.get_incident("INC-1")
        supporting = CorrelationDecision(
            decision_type=DecisionType.ATTACH_EXISTING,
            policy_id="POLICY-WEAK-SUPPORT",
            policy_version="1.0",
            correlation_family=CorrelationFamily.ATTACK_SOURCE,
            reason_code=DecisionReasonCode.UNIQUE_COMPATIBLE_CANDIDATE,
            target_incident_id="INC-1",
            normalized_fingerprint=None,
            anchor_strength=AnchorStrength.STRONG,
        )
        request = _request(
            supporting,
            _event("EVT-2", event_type="supporting_event", detected_at="2026-09-09T12:01:00Z"),
            operation_id="OP-SUPPORT",
            now=NOW + timedelta(minutes=2),
        )
        manager.apply_correlation_mutation(request)
        after = store.get_incident("INC-1")

        assert after.event_ids == ("EVT-1", "EVT-2")
        assert after.anchor_event_id == before.anchor_event_id
        assert after.correlation_context == before.correlation_context


@pytest.mark.parametrize("status", [IncidentStatus.OPEN, IncidentStatus.ASSIGNED, IncidentStatus.IN_PROGRESS])
def test_attach_accepts_each_correlation_open_lifecycle_status(tmp_path, status):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        execute_controlled_sql(
            store,
            "UPDATE incidents SET status = ? WHERE incident_id = 'INC-1'",
            (status.value,),
        )
        manager.apply_correlation_mutation(_attach_request())
        assert store.get_incident("INC-1").status is status


@pytest.mark.parametrize("status", [IncidentStatus.AWAITING_REVIEW, IncidentStatus.CLOSED])
def test_attach_rejects_correlation_closed_lifecycle_without_mutation(tmp_path, status):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        if status is IncidentStatus.CLOSED:
            execute_controlled_sql(
                store,
                "UPDATE incidents SET status = ?, closed_at = ? WHERE incident_id = 'INC-1'",
                (status.value, NOW.isoformat()),
            )
        else:
            execute_controlled_sql(
                store,
                "UPDATE incidents SET status = ? WHERE incident_id = 'INC-1'",
                (status.value,),
            )
        before = store.get_incident("INC-1")
        error = _assert_error(
            IncidentErrorCode.INCIDENT_NOT_CORRELATION_OPEN,
            manager.apply_correlation_mutation,
            _attach_request(),
        )
        assert error.retry_disposition is RetryDisposition.REPAIR_REQUIRED
        assert store.get_incident("INC-1") == before
        assert store.get_operation_result("OP-ATTACH") is None


def test_attach_requires_exact_existing_target(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        _assert_error(
            IncidentErrorCode.INCIDENT_NOT_FOUND,
            manager.apply_correlation_mutation,
            _attach_request(target="INC-MISSING"),
        )
        assert store.get_incident("INC-1").event_ids == ("EVT-1",)


def test_different_operation_cannot_reuse_owned_event_even_for_same_incident(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        request = _attach_request(
            event_id="EVT-1",
            operation_id="OP-OTHER",
            detected_at="2026-09-09T11:59:00Z",
        )
        _assert_error(
            IncidentErrorCode.EVENT_OWNERSHIP_CONFLICT,
            manager.apply_correlation_mutation,
            request,
        )
        assert len(store.get_incident("INC-1").audit_trail) == 1


@pytest.mark.parametrize(
    "mutation_request",
    [
        _attach_request(
            family=CorrelationFamily.DOWNSTREAM_CASCADE,
            fingerprint=None,
        ),
        _attach_request(
            strength=AnchorStrength.WEAK,
            fingerprint=None,
        ),
        _attach_request(fingerprint=_fingerprint(source_ip="192.0.2.99")),
    ],
)
def test_attach_rejects_family_or_anchor_context_conflict(tmp_path, mutation_request):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        _assert_error(
            IncidentErrorCode.CORRELATION_CONTEXT_CONFLICT,
            manager.apply_correlation_mutation,
            mutation_request,
        )
        assert store.get_incident("INC-1").event_ids == ("EVT-1",)


@pytest.mark.parametrize(
    ("mutation_request", "expected_code"),
    [
        (
            _attach_request(detected_at="2026-09-09T11:58:59Z"),
            IncidentErrorCode.STALE_CORRELATION_EVENT_TIME,
        ),
        (
            _attach_request(now=NOW - timedelta(seconds=1)),
            IncidentErrorCode.STALE_CORRELATION_EVENT_TIME,
        ),
    ],
)
def test_attach_rejects_stale_event_or_authoritative_now(tmp_path, mutation_request, expected_code):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        before = store.get_incident("INC-1")
        _assert_error(expected_code, manager.apply_correlation_mutation, mutation_request)
        assert store.get_incident("INC-1") == before


def test_equal_event_and_mutation_time_boundaries_are_allowed(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        request = _attach_request(
            detected_at="2026-09-09T11:59:00Z",
            now=NOW,
        )
        manager.apply_correlation_mutation(request)
        assert store.get_incident("INC-1").event_ids == ("EVT-1", "EVT-2")


def test_attach_never_downgrades_severity_or_emits_false_escalation(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request(severity="CRITICAL"))
        manager.apply_correlation_mutation(_attach_request(severity="LOW"))
        incident = store.get_incident("INC-1")
        assert incident.severity is IncidentSeverity.CRITICAL
        assert incident.audit_trail[-1].effects == (
            IncidentAuditEffect.EVENT_ATTACHED,
        )


def test_attach_preserves_lifecycle_assignment_review_and_rca_relationship(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        execute_controlled_sql(
            store,
            """
            UPDATE incidents
            SET status = 'ASSIGNED', assignee = 'alice', reviewer = 'bob',
                rca_status = 'GENERATING', rca_ref = 'RCA-1',
                external_refs = '["JIRA-1"]'
            WHERE incident_id = 'INC-1'
            """,
        )
        before = store.get_incident("INC-1")
        manager.apply_correlation_mutation(_attach_request())
        after = store.get_incident("INC-1")

        assert after.status is before.status is IncidentStatus.ASSIGNED
        assert (after.assignee, after.reviewer) == ("alice", "bob")
        assert (after.rca_status, after.rca_ref) == ("GENERATING", "RCA-1")
        assert after.external_refs == ("JIRA-1",)
        assert after.created_at == before.created_at


def test_receipt_replay_returns_original_result_without_second_mutation(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        request = _create_request()
        original = manager.apply_correlation_mutation(request)
        retry = replace(request, now=NOW - timedelta(days=1))
        replayed = manager.apply_correlation_mutation(retry)

        assert replayed == original
        incident = store.get_incident("INC-1")
        assert incident.updated_at == NOW
        assert len(incident.event_ids) == len(incident.audit_trail) == 1


def test_same_operation_with_different_projection_is_receipt_conflict(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        contradictory = _create_request(
            operation_id="OP-CREATE",
            severity="CRITICAL",
        )
        _assert_error(
            IncidentErrorCode.MUTATION_RECEIPT_CONFLICT,
            manager.apply_correlation_mutation,
            contradictory,
        )
        assert len(store.get_incident("INC-1").audit_trail) == 1


def test_receipt_conflict_precedes_new_request_projection_validation(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(_create_request())
        contradictory = _create_request(
            operation_id="OP-CREATE",
            fingerprint=_fingerprint("different_event_type"),
        )
        _assert_error(
            IncidentErrorCode.MUTATION_RECEIPT_CONFLICT,
            manager.apply_correlation_mutation,
            contradictory,
        )
        assert len(store.get_incident("INC-1").audit_trail) == 1


def test_failure_during_final_audit_write_rolls_back_all_authorities(tmp_path, monkeypatch):
    store, manager = _manager(tmp_path)

    def fail_audit(self, entry):
        raise RuntimeError("controlled audit write failure")

    monkeypatch.setattr(_IncidentStoreTransaction, "_insert_audit_entry", fail_audit)
    with store:
        with pytest.raises(RuntimeError, match="controlled audit write failure"):
            manager.apply_correlation_mutation(_create_request())
        assert store.get_incident("INC-1") is None
        assert store.get_operation_result("OP-CREATE") is None
        store.validate_integrity()


def test_valid_promotion_is_atomic_after_phase_4_support(tmp_path):
    store, manager = _manager(tmp_path)
    with store:
        manager.apply_correlation_mutation(
            _create_request(strength=AnchorStrength.WEAK)
        )
        promotion = _attach_request(
            transition=AnchorTransition.WEAK_TO_STRONG,
            strength=AnchorStrength.STRONG,
        )
        result = manager.apply_correlation_mutation(promotion)
        replay = manager.apply_correlation_mutation(
            replace(promotion, now=NOW + timedelta(days=1))
        )
        incident = store.get_incident("INC-1")
        assert replay == result == store.get_operation_result("OP-ATTACH")
        assert incident.event_ids == ("EVT-1", "EVT-2")
        assert incident.anchor_event_id == "EVT-2"
        assert incident.correlation_context.anchor_strength is AnchorStrength.STRONG
        assert incident.correlation_context.promoted_from_weak is True
        assert len(incident.audit_trail) == 2
        assert incident.audit_trail[-1].effects == (
            IncidentAuditEffect.EVENT_ATTACHED,
            IncidentAuditEffect.ANCHOR_PROMOTED,
        )
        store.validate_integrity()


def test_route_shadow_and_enter_pending_remain_outside_manager_domain(tmp_path):
    store, manager = _manager(tmp_path)
    route_shadow = CorrelationDecision(
        decision_type=DecisionType.ROUTE_SHADOW,
        policy_id="POLICY-SHADOW",
        policy_version="1.0",
        correlation_family=CorrelationFamily.UNKNOWN,
        reason_code=DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY,
    )
    event = _event()
    intent = CorrelationMutationIntent.from_decision(
        operation_id="OP-SHADOW",
        event_id="EVT-1",
        decision=route_shadow,
        created_at=NOW,
    )
    with store:
        error = _assert_error(
            IncidentErrorCode.INVALID_INCIDENT_MUTATION,
            IncidentMutationRequest,
            intent,
            event,
            NOW,
        )
        assert error.retry_disposition is RetryDisposition.NON_RETRYABLE
        with pytest.raises(StateDomainValidationError):
            CorrelationMutationIntent.from_decision(
                operation_id="OP-PENDING",
                event_id="EVT-1",
                decision=CorrelationDecision(
                    decision_type=DecisionType.ENTER_PENDING,
                    policy_id="POLICY-PENDING",
                    policy_version="1.0",
                    correlation_family=CorrelationFamily.ATTACK_SOURCE,
                    reason_code=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
                ),
                created_at=NOW,
            )
        assert store.list_correlation_views() == ()
