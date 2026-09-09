from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

import src.incident_management.contracts as incident_contracts
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
    StateDomainErrorCode,
    StateDomainValidationError,
    TerminalOutcome,
)
from src.incident_management import (
    CORRELATION_CLOSED_STATUSES,
    CORRELATION_OPEN_STATUSES,
    INCIDENT_ERROR_DISPOSITIONS,
    RCA_INITIAL_STATUS,
    IncidentAuditAction,
    IncidentAuditEffect,
    IncidentAuditEntry,
    IncidentCorrelationContext,
    IncidentDomainError,
    IncidentErrorCode,
    IncidentMutationRequest,
    IncidentOperationReceipt,
    IncidentOperationResult,
    IncidentMutationCompletion,
    IncidentRecord,
    IncidentSeverity,
    IncidentStatus,
    OperationReceiptSemanticIdentity,
    event_mutation_projection,
)


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
DETECTED_AT = "2026-09-09T11:59:00Z"
FINGERPRINT = NormalizedFingerprint.from_mapping(
    "brute_force_detected", {"source_ip": "192.0.2.10"}
)


def _decision(decision_type=DecisionType.CREATE_NEW):
    if decision_type is DecisionType.CREATE_NEW:
        return CorrelationDecision(
            decision_type=decision_type,
            policy_id="POLICY-BRUTE-FORCE",
            policy_version="1.0",
            correlation_family=CorrelationFamily.ATTACK_SOURCE,
            reason_code=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
            normalized_fingerprint=FINGERPRINT,
            anchor_strength=AnchorStrength.STRONG,
        )
    if decision_type is DecisionType.ROUTE_SHADOW:
        return CorrelationDecision(
            decision_type=decision_type,
            policy_id="POLICY-UNKNOWN",
            policy_version="1.0",
            correlation_family=CorrelationFamily.UNKNOWN,
            reason_code=DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY,
        )
    if decision_type is DecisionType.ENTER_PENDING:
        return CorrelationDecision(
            decision_type=decision_type,
            policy_id="POLICY-WEAK",
            policy_version="1.0",
            correlation_family=CorrelationFamily.DOWNSTREAM_CASCADE,
            reason_code=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
        )
    raise AssertionError("unsupported test decision")


def _weak_create_decision():
    return CorrelationDecision(
        decision_type=DecisionType.CREATE_NEW,
        policy_id="POLICY-WEAK",
        policy_version="1.0",
        correlation_family=CorrelationFamily.DOWNSTREAM_CASCADE,
        reason_code=DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
        normalized_fingerprint=None,
        anchor_strength=AnchorStrength.WEAK,
    )


def _attach_decision():
    return CorrelationDecision(
        decision_type=DecisionType.ATTACH_EXISTING,
        policy_id="POLICY-BRUTE-FORCE",
        policy_version="1.0",
        correlation_family=CorrelationFamily.ATTACK_SOURCE,
        reason_code=DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH,
        target_incident_id="INC-1",
        normalized_fingerprint=FINGERPRINT,
        anchor_strength=AnchorStrength.STRONG,
        anchor_transition=AnchorTransition.NONE,
    )


def _intent(decision_type=DecisionType.CREATE_NEW, **changes):
    values = {
        "operation_id": "OP-1",
        "event_id": "EVT-1",
        "decision": _decision(decision_type),
        "created_at": NOW - timedelta(minutes=2),
    }
    values.update(changes)
    return CorrelationMutationIntent.from_decision(**values)


def _event(**changes):
    value = {
        "event_id": "EVT-1",
        "event_type": "brute_force_detected",
        "detected_at": DETECTED_AT,
        "severity": "CRITICAL",
        # Remaining authoritative Event fields are deliberately opaque to 008.
        "event_source": "log_event_detection",
        "detection_method": "rule_based",
    }
    value.update(changes)
    return value


def _context(strength=AnchorStrength.STRONG):
    if strength is AnchorStrength.WEAK:
        return IncidentCorrelationContext(
            CorrelationFamily.DOWNSTREAM_CASCADE,
            AnchorStrength.WEAK,
            None,
            None,
            None,
            "POLICY-WEAK",
            "1.0",
        )
    return IncidentCorrelationContext(
        CorrelationFamily.ATTACK_SOURCE,
        AnchorStrength.STRONG,
        "EVT-1",
        "brute_force_detected",
        FINGERPRINT,
        "POLICY-BRUTE-FORCE",
        "1.0",
    )


def _audit():
    return IncidentAuditEntry(
        "OP-1",
        "EVT-1",
        "INC-1",
        "POLICY-BRUTE-FORCE",
        "1.0",
        DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
        IncidentAuditAction.INCIDENT_CREATED,
        (IncidentAuditEffect.EVENT_ATTACHED,),
        NOW,
    )


def _record(**changes):
    values = {
        "incident_id": "INC-1",
        "event_ids": ("EVT-1",),
        "anchor_event_id": "EVT-1",
        "status": IncidentStatus.OPEN,
        "severity": IncidentSeverity.CRITICAL,
        "created_at": NOW,
        "updated_at": NOW,
        "last_correlated_at": NOW - timedelta(minutes=1),
        "closed_at": None,
        "assignee": None,
        "reviewer": None,
        "correlation_context": _context(),
        "audit_trail": (_audit(),),
        "rca_status": RCA_INITIAL_STATUS,
        "rca_ref": None,
        "external_refs": (),
    }
    values.update(changes)
    return IncidentRecord(**values)


def _assert_code(code, callable_, *args, **kwargs):
    with pytest.raises(IncidentDomainError) as raised:
        callable_(*args, **kwargs)
    assert raised.value.code is code
    return raised.value


def test_valid_strong_and_weak_incident_records_and_initial_rca_state():
    strong = _record()
    weak = _record(
        anchor_event_id=None,
        correlation_context=_context(AnchorStrength.WEAK),
        severity=IncidentSeverity.LOW,
        rca_status="PENDING",
        rca_ref=None,
    )

    assert strong.status is IncidentStatus.OPEN
    assert strong.rca_status == RCA_INITIAL_STATUS == "PENDING"
    assert strong.rca_ref is None
    assert weak.anchor_event_id is None
    assert weak.correlation_context.normalized_fingerprint is None


@pytest.mark.parametrize(
    "changes",
    [
        {"event_ids": ()},
        {"event_ids": ("EVT-1", "EVT-1")},
        {"anchor_event_id": "EVT-OTHER"},
        {"status": "OPEN"},
        {"severity": "CRITICAL"},
        {"updated_at": NOW - timedelta(seconds=1)},
        {"closed_at": NOW},
        {"audit_trail": (replace(_audit(), incident_id="INC-OTHER"),)},
        {"audit_trail": ()},
        {"external_refs": ("JIRA-1", "JIRA-1")},
    ],
)
def test_invalid_incident_record_fails_closed(changes):
    _assert_code(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, _record, **changes)


def test_lifecycle_sets_are_closed_and_partition_all_statuses():
    assert CORRELATION_OPEN_STATUSES == {
        IncidentStatus.OPEN,
        IncidentStatus.ASSIGNED,
        IncidentStatus.IN_PROGRESS,
    }
    assert CORRELATION_CLOSED_STATUSES == {
        IncidentStatus.AWAITING_REVIEW,
        IncidentStatus.CLOSED,
    }
    assert CORRELATION_OPEN_STATUSES.isdisjoint(CORRELATION_CLOSED_STATUSES)
    assert CORRELATION_OPEN_STATUSES | CORRELATION_CLOSED_STATUSES == set(IncidentStatus)


def test_severity_is_the_exact_closed_monotonic_order():
    assert [severity.name for severity in IncidentSeverity] == [
        "LOW",
        "MEDIUM",
        "HIGH",
        "CRITICAL",
    ]
    assert IncidentSeverity.LOW < IncidentSeverity.MEDIUM < IncidentSeverity.HIGH < IncidentSeverity.CRITICAL


@pytest.mark.parametrize(
    "changes",
    [
        {"anchor_event_id": None},
        {"anchor_event_type": None},
        {"normalized_fingerprint": None},
        {"anchor_policy_id": None},
        {"anchor_policy_version": None},
    ],
)
def test_strong_context_requires_complete_anchor(changes):
    values = {
        "correlation_family": CorrelationFamily.ATTACK_SOURCE,
        "anchor_strength": AnchorStrength.STRONG,
        "anchor_event_id": "EVT-1",
        "anchor_event_type": "brute_force_detected",
        "normalized_fingerprint": FINGERPRINT,
        "anchor_policy_id": "POLICY-BRUTE-FORCE",
        "anchor_policy_version": "1.0",
    }
    values.update(changes)
    _assert_code(
        IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
        IncidentCorrelationContext,
        **values,
    )


def test_coherent_strong_context_is_accepted():
    context = _context()
    assert context.anchor_event_type == context.normalized_fingerprint.event_type


def test_strong_context_rejects_anchor_event_type_fingerprint_mismatch():
    mismatched = NormalizedFingerprint.from_mapping(
        "different_event_type", {"source_ip": "192.0.2.10"}
    )
    _assert_code(
        IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
        IncidentCorrelationContext,
        CorrelationFamily.ATTACK_SOURCE,
        AnchorStrength.STRONG,
        "EVT-1",
        "brute_force_detected",
        mismatched,
        "POLICY-BRUTE-FORCE",
        "1.0",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"anchor_event_id": "EVT-1"},
        {"anchor_event_type": "brute_force_detected"},
        {"normalized_fingerprint": FINGERPRINT},
        {"anchor_policy_id": None},
        {"anchor_policy_version": None},
        {"promoted_from_weak": True},
    ],
)
def test_weak_standalone_requires_null_anchor_fields(changes):
    values = {
        "correlation_family": CorrelationFamily.DOWNSTREAM_CASCADE,
        "anchor_strength": AnchorStrength.WEAK,
        "anchor_event_id": None,
        "anchor_event_type": None,
        "normalized_fingerprint": None,
        "anchor_policy_id": "POLICY-WEAK",
        "anchor_policy_version": "1.0",
        "promoted_from_weak": False,
    }
    values.update(changes)
    _assert_code(
        IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
        IncidentCorrelationContext,
        **values,
    )


def test_top_level_and_context_anchor_must_be_coherent():
    _assert_code(
        IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
        _record,
        anchor_event_id=None,
    )


@pytest.mark.parametrize("field_name", ["created_at", "updated_at", "last_correlated_at"])
def test_incident_timestamps_must_be_timezone_aware(field_name):
    _assert_code(
        IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
        _record,
        **{field_name: NOW.replace(tzinfo=None)},
    )


def test_request_is_thin_uses_mapping_and_does_not_mutate_event():
    intent = _intent()
    event = _event()
    before = dict(event)
    request = IncidentMutationRequest(intent, event, NOW.astimezone(timezone(timedelta(hours=8))))

    assert request.intent is intent
    assert request.event is event
    assert event == before
    assert request.now == NOW
    assert [field.name for field in fields(IncidentMutationRequest)] == ["intent", "event", "now"]


def test_valid_strong_weak_create_and_attach_intent_semantics_are_accepted():
    strong_create = _intent()
    weak_create = _intent(decision=_weak_create_decision(), operation_id="OP-WEAK")
    attach = _intent(decision=_attach_decision(), operation_id="OP-ATTACH")

    assert IncidentMutationRequest(strong_create, _event(), NOW).intent is strong_create
    assert IncidentMutationRequest(weak_create, _event(), NOW).intent is weak_create
    assert IncidentMutationRequest(attach, _event(), NOW).intent is attach


@pytest.mark.parametrize(
    "changes",
    [
        {"anchor_strength": None, "normalized_fingerprint": None},
        {"anchor_strength": AnchorStrength.STRONG, "normalized_fingerprint": None},
        {
            "anchor_strength": AnchorStrength.WEAK,
            "normalized_fingerprint": FINGERPRINT,
            "reason_code": DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
        },
        {"anchor_transition": AnchorTransition.WEAK_TO_STRONG},
    ],
)
def test_request_rejects_real_intent_instances_with_invalid_static_semantics(changes):
    valid = _intent()
    malformed = replace(valid, **changes)
    error = _assert_code(
        IncidentErrorCode.INVALID_INCIDENT_MUTATION,
        IncidentMutationRequest,
        malformed,
        _event(),
        NOW,
    )
    assert error.retry_disposition is RetryDisposition.NON_RETRYABLE


def test_event_projection_is_only_four_fields_and_checks_event_identity():
    projection = event_mutation_projection(MappingProxyType(_event()))
    assert projection == (
        "EVT-1",
        "brute_force_detected",
        datetime(2026, 9, 9, 11, 59, tzinfo=timezone.utc),
        IncidentSeverity.CRITICAL,
    )
    _assert_code(
        IncidentErrorCode.INVALID_INCIDENT_MUTATION,
        IncidentMutationRequest,
        _intent(),
        _event(event_id="EVT-OTHER"),
        NOW,
    )


@pytest.mark.parametrize(
    "event",
    [
        _event(detected_at="2026-09-09T11:59:00"),
        _event(detected_at="2026-09-09T19:59:00+08:00"),
        _event(severity="SEVERE"),
        _event(event_type=""),
    ],
)
def test_event_projection_rejects_invalid_prd_002_fields(event):
    _assert_code(IncidentErrorCode.INVALID_INCIDENT_MUTATION, event_mutation_projection, event)


def test_request_now_must_be_timezone_aware():
    _assert_code(
        IncidentErrorCode.INVALID_INCIDENT_MUTATION,
        IncidentMutationRequest,
        _intent(),
        _event(),
        NOW.replace(tzinfo=None),
    )


def test_route_shadow_is_explicitly_wrong_domain_and_non_retryable():
    error = _assert_code(
        IncidentErrorCode.INVALID_INCIDENT_MUTATION,
        IncidentMutationRequest,
        _intent(DecisionType.ROUTE_SHADOW),
        _event(),
        NOW,
    )
    assert error.retry_disposition is RetryDisposition.NON_RETRYABLE


def test_enter_pending_cannot_create_real_spec_007_mutation_intent():
    with pytest.raises(StateDomainValidationError) as raised:
        _intent(DecisionType.ENTER_PENDING)
    assert raised.value.code is StateDomainErrorCode.MUTATION_INTENT_CONFLICT


def test_receipt_identity_has_complete_intent_and_event_projection_but_excludes_retry_now():
    first = IncidentMutationRequest(_intent(), _event(), NOW)
    retry = IncidentMutationRequest(_intent(), _event(), NOW + timedelta(days=1))
    identity = OperationReceiptSemanticIdentity.from_request(first)

    assert identity == OperationReceiptSemanticIdentity.from_request(retry)
    assert {field.name for field in fields(identity)} == {
        "event_id",
        "intended_terminal_outcome",
        "decision_type",
        "policy_id",
        "policy_version",
        "correlation_family",
        "reason_code",
        "target_incident_id",
        "normalized_fingerprint",
        "anchor_strength",
        "anchor_transition",
        "intent_created_at",
        "event_type",
        "event_detected_at",
        "event_severity",
    }
    assert identity.intended_terminal_outcome is TerminalOutcome.CREATED_INCIDENT
    assert identity.normalized_fingerprint is FINGERPRINT

    different_event = IncidentMutationRequest(_intent(), _event(severity="HIGH"), NOW)
    assert identity != OperationReceiptSemanticIdentity.from_request(different_event)


def test_operation_receipt_requires_result_and_identity_coherence():
    request = IncidentMutationRequest(_intent(), _event(), NOW)
    identity = OperationReceiptSemanticIdentity.from_request(request)
    result = IncidentOperationResult(
        "OP-1",
        "EVT-1",
        DecisionType.CREATE_NEW,
        "INC-1",
        IncidentMutationCompletion.SUCCEEDED,
        NOW,
    )
    assert IncidentOperationReceipt(result, identity).result.incident_id == "INC-1"
    _assert_code(
        IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
        IncidentOperationReceipt,
        replace(result, event_id="EVT-OTHER"),
        identity,
    )


def test_full_twelve_code_error_disposition_mapping_is_closed():
    assert isinstance(INCIDENT_ERROR_DISPOSITIONS, MappingProxyType)
    assert set(INCIDENT_ERROR_DISPOSITIONS) == set(IncidentErrorCode)
    assert len(IncidentErrorCode) == 12
    assert INCIDENT_ERROR_DISPOSITIONS[IncidentErrorCode.INVALID_INCIDENT_MUTATION] is RetryDisposition.NON_RETRYABLE
    assert INCIDENT_ERROR_DISPOSITIONS[IncidentErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE] is RetryDisposition.RETRYABLE
    repair_codes = set(IncidentErrorCode) - {
        IncidentErrorCode.INVALID_INCIDENT_MUTATION,
        IncidentErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE,
    }
    assert {INCIDENT_ERROR_DISPOSITIONS[code] for code in repair_codes} == {RetryDisposition.REPAIR_REQUIRED}


def test_real_upstream_types_are_reused_and_no_event_or_correlation_duplicates_exist():
    assert incident_contracts.AnchorStrength is AnchorStrength
    assert incident_contracts.AnchorTransition is AnchorTransition
    assert incident_contracts.CorrelationFamily is CorrelationFamily
    assert incident_contracts.DecisionReasonCode is DecisionReasonCode
    assert incident_contracts.DecisionType is DecisionType
    assert incident_contracts.NormalizedFingerprint is NormalizedFingerprint
    assert incident_contracts.CorrelationMutationIntent is CorrelationMutationIntent
    assert incident_contracts.RetryDisposition is RetryDisposition
    assert incident_contracts.TerminalOutcome is TerminalOutcome

    forbidden = {
        "Event",
        "RuntimeEvent",
        "EventRecord",
        "CorrelationDecisionType",
        "IncidentDecisionType",
        "IncidentCorrelationFamily",
        "IncidentAnchorStrength",
        "IncidentAnchorTransition",
        "IncidentNormalizedFingerprint",
        "IncidentMutationIntent",
    }
    assert forbidden.isdisjoint(vars(incident_contracts))
