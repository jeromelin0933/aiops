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
from src.alert_correlation.state import CorrelationMutationIntent
from src.incident_management import (
    IncidentAuditEffect,
    IncidentDomainError,
    IncidentErrorCode,
    IncidentManager,
    IncidentMutationRequest,
    IncidentSeverity,
    SqliteIncidentStore,
)


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _fingerprint(
    event_type="brute_force_detected", source_ip="192.0.2.10"
):
    return NormalizedFingerprint.from_mapping(event_type, {"source_ip": source_ip})


def _event(
    event_id="EVT-1",
    *,
    event_type="brute_force_detected",
    detected_at="2026-09-09T11:59:00Z",
    severity="LOW",
):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "detected_at": detected_at,
        "severity": severity,
    }


def _request(decision, event, operation_id, *, now=NOW, created_at=None):
    intent = CorrelationMutationIntent.from_decision(
        operation_id=operation_id,
        event_id=event["event_id"],
        decision=decision,
        created_at=created_at or NOW - timedelta(minutes=5),
    )
    return IncidentMutationRequest(intent, event, now)


def _create_request(
    *,
    operation_id="OP-CREATE",
    event_id="EVT-1",
    strength=AnchorStrength.STRONG,
    event_type="brute_force_detected",
    detected_at="2026-09-09T11:59:00Z",
    severity="LOW",
    policy_id="POLICY-CREATE",
    policy_version="1.0",
    family=CorrelationFamily.ATTACK_SOURCE,
    reason=None,
    fingerprint=None,
    now=NOW,
    created_at=None,
):
    if reason is None:
        reason = (
            DecisionReasonCode.NO_COMPATIBLE_CANDIDATE
            if strength is AnchorStrength.STRONG
            else DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED
        )
    if fingerprint is None and strength is AnchorStrength.STRONG:
        fingerprint = _fingerprint(event_type)
    decision = CorrelationDecision(
        decision_type=DecisionType.CREATE_NEW,
        policy_id=policy_id,
        policy_version=policy_version,
        correlation_family=family,
        reason_code=reason,
        normalized_fingerprint=fingerprint,
        anchor_strength=strength,
    )
    return _request(
        decision,
        _event(
            event_id,
            event_type=event_type,
            detected_at=detected_at,
            severity=severity,
        ),
        operation_id,
        now=now,
        created_at=created_at,
    )


def _promotion_request(
    *,
    operation_id="OP-PROMOTE",
    event_id="EVT-STRONG",
    target="INC-1",
    family=CorrelationFamily.ATTACK_SOURCE,
    detected_at="2026-09-09T12:01:00Z",
    severity="CRITICAL",
    fingerprint=None,
    now=NOW + timedelta(minutes=2),
):
    fingerprint = fingerprint or _fingerprint()
    decision = CorrelationDecision(
        decision_type=DecisionType.ATTACH_EXISTING,
        policy_id="POLICY-STRONG",
        policy_version="2.0",
        correlation_family=family,
        reason_code=DecisionReasonCode.WEAK_TO_STRONG_PROMOTION,
        target_incident_id=target,
        normalized_fingerprint=fingerprint,
        anchor_strength=AnchorStrength.STRONG,
        anchor_transition=AnchorTransition.WEAK_TO_STRONG,
    )
    return _request(
        decision,
        _event(
            event_id,
            detected_at=detected_at,
            severity=severity,
        ),
        operation_id,
        now=now,
    )


def _manager(path):
    store = SqliteIncidentStore(str(path))
    return store, IncidentManager(store, incident_id_factory=lambda: "INC-1")


def _assert_error(code, callable_, *args):
    with pytest.raises(IncidentDomainError) as raised:
        callable_(*args)
    assert raised.value.code is code
    return raised.value


def test_equivalent_replay_preserves_original_result_timestamps_and_single_audit(tmp_path):
    store, manager = _manager(tmp_path / "replay.db")
    request = _create_request()
    with store:
        original = manager.apply_correlation_mutation(request)
        replay = manager.apply_correlation_mutation(
            replace(request, now=NOW - timedelta(days=30))
        )
        incident = store.get_incident("INC-1")

        assert replay == original
        assert original.completed_at == NOW
        assert incident.created_at == incident.updated_at == NOW
        assert incident.last_correlated_at == NOW - timedelta(minutes=1)
        assert incident.event_ids == ("EVT-1",)
        assert len(incident.audit_trail) == 1
        assert incident.audit_trail[0].occurred_at == NOW


def _contradictory_requests():
    return [
        _create_request(event_id="EVT-DIFFERENT"),
        _create_request(event_type="different_event_type"),
        _create_request(detected_at="2026-09-09T11:58:00Z"),
        _create_request(severity="HIGH"),
        _create_request(policy_id="POLICY-DIFFERENT"),
        _create_request(policy_version="2.0"),
        _create_request(family=CorrelationFamily.DOWNSTREAM_CASCADE),
        _create_request(reason=DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED),
        _create_request(fingerprint=_fingerprint(source_ip="192.0.2.99")),
        _create_request(
            strength=AnchorStrength.WEAK,
            reason=DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
        ),
        _create_request(created_at=NOW - timedelta(minutes=6)),
    ]


@pytest.mark.parametrize("contradictory", _contradictory_requests())
def test_same_operation_full_semantic_contradiction_is_receipt_conflict(
    tmp_path, contradictory
):
    store, manager = _manager(tmp_path / "conflict.db")
    with store:
        manager.apply_correlation_mutation(_create_request())
        error = _assert_error(
            IncidentErrorCode.MUTATION_RECEIPT_CONFLICT,
            manager.apply_correlation_mutation,
            contradictory,
        )
        assert error.retry_disposition.value == "REPAIR_REQUIRED"
        assert store.get_incident("INC-1").event_ids == ("EVT-1",)
        assert len(store.get_incident("INC-1").audit_trail) == 1


def test_same_operation_changed_decision_outcome_target_and_transition_conflicts(tmp_path):
    store, manager = _manager(tmp_path / "decision-conflict.db")
    with store:
        manager.apply_correlation_mutation(_create_request())
        attach = CorrelationDecision(
            decision_type=DecisionType.ATTACH_EXISTING,
            policy_id="POLICY-STRONG",
            policy_version="1.0",
            correlation_family=CorrelationFamily.ATTACK_SOURCE,
            reason_code=DecisionReasonCode.WEAK_TO_STRONG_PROMOTION,
            target_incident_id="INC-1",
            normalized_fingerprint=_fingerprint(),
            anchor_strength=AnchorStrength.STRONG,
            anchor_transition=AnchorTransition.WEAK_TO_STRONG,
        )
        contradictory = _request(
            attach, _event(), "OP-CREATE", now=NOW + timedelta(minutes=1)
        )
        _assert_error(
            IncidentErrorCode.MUTATION_RECEIPT_CONFLICT,
            manager.apply_correlation_mutation,
            contradictory,
        )


def test_same_event_with_different_operation_is_ownership_conflict(tmp_path):
    store, manager = _manager(tmp_path / "ownership.db")
    with store:
        manager.apply_correlation_mutation(_create_request())
        different_operation = _create_request(operation_id="OP-DIFFERENT")
        _assert_error(
            IncidentErrorCode.EVENT_OWNERSHIP_CONFLICT,
            manager.apply_correlation_mutation,
            different_operation,
        )
        assert store.get_operation_result("OP-DIFFERENT") is None


def test_crash_c_reopen_lookup_and_replay_return_same_durable_result(tmp_path):
    path = tmp_path / "crash-c.db"
    request = _create_request()
    store, manager = _manager(path)
    original = manager.apply_correlation_mutation(request)
    store.close()  # Simulates crash after 008 commit and before 007 Processed.

    reopened, recovered_manager = _manager(path)
    with reopened:
        assert reopened.get_operation_result("OP-CREATE") == original
        replay = recovered_manager.apply_correlation_mutation(
            replace(request, now=NOW + timedelta(days=1))
        )
        assert replay == original
        incident = reopened.get_incident("INC-1")
        assert incident.event_ids == ("EVT-1",)
        assert len(incident.audit_trail) == 1


def test_valid_late_strong_promotion_preserves_identity_and_updates_anchor(tmp_path):
    store, manager = _manager(tmp_path / "promotion.db")
    with store:
        manager.apply_correlation_mutation(
            _create_request(strength=AnchorStrength.WEAK, severity="LOW")
        )
        before = store.get_incident("INC-1")
        result = manager.apply_correlation_mutation(_promotion_request())
        promoted = store.get_incident("INC-1")

        assert result.incident_id == before.incident_id == promoted.incident_id == "INC-1"
        assert promoted.event_ids == ("EVT-1", "EVT-STRONG")
        assert promoted.correlation_context.correlation_family is before.correlation_context.correlation_family
        assert promoted.anchor_event_id == "EVT-STRONG"
        assert promoted.correlation_context.anchor_event_id == "EVT-STRONG"
        assert promoted.correlation_context.anchor_event_type == "brute_force_detected"
        assert promoted.correlation_context.anchor_strength is AnchorStrength.STRONG
        assert promoted.correlation_context.normalized_fingerprint == _fingerprint()
        assert promoted.correlation_context.anchor_policy_id == "POLICY-STRONG"
        assert promoted.correlation_context.anchor_policy_version == "2.0"
        assert promoted.correlation_context.promoted_from_weak is True
        assert promoted.severity is IncidentSeverity.CRITICAL
        assert promoted.updated_at == NOW + timedelta(minutes=2)
        assert promoted.last_correlated_at == NOW + timedelta(minutes=1)
        assert promoted.audit_trail[-1].effects == (
            IncidentAuditEffect.EVENT_ATTACHED,
            IncidentAuditEffect.ANCHOR_PROMOTED,
            IncidentAuditEffect.SEVERITY_ESCALATED,
        )
        store.validate_integrity()


def test_promotion_replay_does_not_repeat_anchor_audit_or_timestamps(tmp_path):
    store, manager = _manager(tmp_path / "promotion-replay.db")
    with store:
        manager.apply_correlation_mutation(
            _create_request(strength=AnchorStrength.WEAK)
        )
        request = _promotion_request()
        original = manager.apply_correlation_mutation(request)
        replay = manager.apply_correlation_mutation(
            replace(request, now=NOW - timedelta(days=1))
        )
        promoted = store.get_incident("INC-1")

        assert replay == original
        assert promoted.updated_at == NOW + timedelta(minutes=2)
        assert promoted.event_ids == ("EVT-1", "EVT-STRONG")
        assert len(promoted.audit_trail) == 2


def test_second_promotion_with_new_operation_fails_closed(tmp_path):
    store, manager = _manager(tmp_path / "second-promotion.db")
    with store:
        manager.apply_correlation_mutation(
            _create_request(strength=AnchorStrength.WEAK)
        )
        manager.apply_correlation_mutation(_promotion_request())
        before = store.get_incident("INC-1")
        _assert_error(
            IncidentErrorCode.INVALID_ANCHOR_PROMOTION,
            manager.apply_correlation_mutation,
            _promotion_request(
                operation_id="OP-PROMOTE-2",
                event_id="EVT-STRONG-2",
                detected_at="2026-09-09T12:02:00Z",
                now=NOW + timedelta(minutes=3),
            ),
        )
        assert store.get_incident("INC-1") == before
        assert store.get_operation_result("OP-PROMOTE-2") is None


def test_promotion_specific_and_general_failures_preserve_weak_incident(tmp_path):
    store, manager = _manager(tmp_path / "invalid-promotion.db")
    with store:
        manager.apply_correlation_mutation(
            _create_request(strength=AnchorStrength.WEAK)
        )
        before = store.get_incident("INC-1")

        _assert_error(
            IncidentErrorCode.CORRELATION_CONTEXT_CONFLICT,
            manager.apply_correlation_mutation,
            _promotion_request(family=CorrelationFamily.DOWNSTREAM_CASCADE),
        )
        _assert_error(
            IncidentErrorCode.STALE_CORRELATION_EVENT_TIME,
            manager.apply_correlation_mutation,
            _promotion_request(detected_at="2026-09-09T11:58:00Z"),
        )
        assert store.get_incident("INC-1") == before
        assert store.get_operation_result("OP-PROMOTE") is None
