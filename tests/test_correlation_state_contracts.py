from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from src.alert_correlation import (
    DEFAULT_POLICY_REGISTRY,
    AnchorStrength,
    AnchorTransition,
    CorrelationDecision,
    CorrelationErrorCode,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    EvaluationPhase,
    NormalizedFingerprint,
)
from src.alert_correlation.state import (
    ActivePendingRecord,
    BlockedCorrelationRecord,
    CorrelationMutationIntent,
    CorrelationPolicyKind,
    FailureKind,
    PendingGraceConfig,
    PendingReason,
    ProcessedCorrelationRecord,
    RetryDisposition,
    StateDomainErrorCode,
    StateDomainValidationError,
    TerminalOutcome,
    terminal_outcome_for_decision,
    validate_active_pending_record,
)


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def _pending(*, policy_id="POLICY-BRUTE-FORCE-DETECTED", policy_version="1.0", kind=CorrelationPolicyKind.STRONG_ANCHOR, reason=PendingReason.NO_COMPATIBLE_CANDIDATE):
    return ActivePendingRecord("EVT-1", NOW, NOW + timedelta(seconds=30), kind, policy_id, policy_version, reason)


def _decision(kind, *, target=None):
    values = {
        DecisionType.ATTACH_EXISTING: (DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH, CorrelationFamily.ATTACK_SOURCE, AnchorStrength.STRONG, NormalizedFingerprint("brute_force_detected", (("source_ip", "1.2.3.4"),))),
        DecisionType.CREATE_NEW: (DecisionReasonCode.NO_COMPATIBLE_CANDIDATE, CorrelationFamily.ATTACK_SOURCE, AnchorStrength.STRONG, NormalizedFingerprint("brute_force_detected", (("source_ip", "1.2.3.4"),))),
        DecisionType.ROUTE_SHADOW: (DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY, CorrelationFamily.UNKNOWN, None, None),
        DecisionType.ENTER_PENDING: (DecisionReasonCode.NO_COMPATIBLE_CANDIDATE, CorrelationFamily.ATTACK_SOURCE, None, None),
    }
    reason, family, strength, fingerprint = values[kind]
    return CorrelationDecision(kind, "POLICY-BRUTE-FORCE-DETECTED", "1.0", family, reason, target, fingerprint, strength, AnchorTransition.NONE, (("not_authoritative", "ignored"),))


@pytest.mark.parametrize("value", [1, 0.5, 30, 30.25])
def test_pending_grace_accepts_finite_positive_numbers(value):
    assert PendingGraceConfig(value).pending_grace_seconds == float(value)


@pytest.mark.parametrize("value", [True, False, "30", None, 0, -1, float("nan"), float("inf")])
def test_pending_grace_rejects_invalid_values(value):
    with pytest.raises(StateDomainValidationError):
        PendingGraceConfig(value)


def test_active_pending_validates_strong_and_known_weak_exact_policies():
    strong = _pending()
    known_weak = _pending(policy_id="POLICY-HIGH-LATENCY-DETECTED", kind=CorrelationPolicyKind.WEAK_SUPPORTING_KNOWN)
    for record in (strong, known_weak):
        validate_active_pending_record(record, DEFAULT_POLICY_REGISTRY.resolve_exact)


def test_unknown_or_mismatched_pending_policy_fails_closed():
    unknown = _pending(policy_id="POLICY-GENERAL-LOG-ANOMALY", kind=CorrelationPolicyKind.STRONG_ANCHOR)
    mismatch = _pending(policy_id="POLICY-HIGH-LATENCY-DETECTED", kind=CorrelationPolicyKind.STRONG_ANCHOR)
    for record in (unknown, mismatch):
        with pytest.raises(StateDomainValidationError):
            validate_active_pending_record(record, DEFAULT_POLICY_REGISTRY.resolve_exact)


@pytest.mark.parametrize("reason", ["NO_COMPATIBLE_CANDIDATE", None, DecisionReasonCode.NO_COMPATIBLE_CANDIDATE])
def test_invalid_pending_reason_is_rejected(reason):
    with pytest.raises(StateDomainValidationError):
        _pending(reason=reason)


@pytest.mark.parametrize("policy_id,policy_version", [("", "1.0"), (" POLICY", "1.0"), ("POLICY", ""), ("POLICY", " 1.0")])
def test_empty_or_malformed_policy_identity_is_rejected(policy_id, policy_version):
    with pytest.raises(StateDomainValidationError):
        _pending(policy_id=policy_id, policy_version=policy_version)


def test_pending_fields_are_immutable_and_reason_only_updates_from_enter_pending():
    record = _pending()
    changed = record.with_pending_reason_from_decision(
        CorrelationDecision(DecisionType.ENTER_PENDING, "POLICY-BRUTE-FORCE-DETECTED", "1.0", CorrelationFamily.ATTACK_SOURCE, DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES)
    )
    assert changed.pending_reason is PendingReason.MULTIPLE_COMPATIBLE_CANDIDATES
    assert changed.entered_pending_at == record.entered_pending_at
    assert changed.expires_at == record.expires_at
    with pytest.raises(FrozenInstanceError):
        record.expires_at = NOW
    with pytest.raises(StateDomainValidationError):
        record.with_pending_reason_from_decision(_decision(DecisionType.CREATE_NEW))


def test_state_domain_block_allows_none_phase_and_real_evaluation_phase_is_preserved():
    state_block = BlockedCorrelationRecord("EVT-1", FailureKind.STATE_DOMAIN_FAILURE, StateDomainErrorCode.MALFORMED_STATE_RECORD, None, NOW, NOW, 1, RetryDisposition.REPAIR_REQUIRED)
    evaluation_block = BlockedCorrelationRecord("EVT-2", FailureKind.CORRELATION_DOMAIN_FAILURE, CorrelationErrorCode.INVALID_INCIDENT_VIEW, EvaluationPhase.PENDING_RECHECK, NOW, NOW, 1, RetryDisposition.RETRYABLE, "POLICY-BRUTE-FORCE-DETECTED", "1.0")
    assert state_block.evaluation_phase is None
    assert evaluation_block.evaluation_phase is EvaluationPhase.PENDING_RECHECK


def test_processed_reference_shape_requires_the_correct_terminal_reference():
    ProcessedCorrelationRecord("EVT-1", TerminalOutcome.ATTACHED_TO_INCIDENT, NOW, "INC-1", None, "POLICY", "1.0")
    ProcessedCorrelationRecord("EVT-2", TerminalOutcome.SHADOWED, NOW, None, "SHADOW-1", "POLICY", "1.0")
    with pytest.raises(StateDomainValidationError):
        ProcessedCorrelationRecord("EVT-3", TerminalOutcome.SHADOWED, NOW, None, None, "POLICY", "1.0")
    with pytest.raises(StateDomainValidationError):
        ProcessedCorrelationRecord("EVT-4", TerminalOutcome.CREATED_INCIDENT, NOW, None, None, "POLICY", "1.0")


@pytest.mark.parametrize(("decision_type", "outcome"), [(DecisionType.ATTACH_EXISTING, TerminalOutcome.ATTACHED_TO_INCIDENT), (DecisionType.CREATE_NEW, TerminalOutcome.CREATED_INCIDENT), (DecisionType.ROUTE_SHADOW, TerminalOutcome.SHADOWED)])
def test_mutation_intent_uses_spec_006_decision_mapping(decision_type, outcome):
    decision = _decision(decision_type, target="INC-1" if decision_type is DecisionType.ATTACH_EXISTING else None)
    intent = CorrelationMutationIntent.from_decision(operation_id="OP-1", event_id="EVT-1", decision=decision, created_at=NOW)
    assert intent.intended_terminal_outcome is outcome
    assert not hasattr(intent, "diagnostics")


def test_attach_requires_target_and_new_or_shadow_require_null_target():
    with pytest.raises(StateDomainValidationError):
        CorrelationMutationIntent("OP-1", "EVT-1", TerminalOutcome.ATTACHED_TO_INCIDENT, DecisionType.ATTACH_EXISTING, "POLICY", "1.0", CorrelationFamily.ATTACK_SOURCE, DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH, None, None, AnchorStrength.STRONG, AnchorTransition.NONE, NOW)
    with pytest.raises(StateDomainValidationError):
        CorrelationMutationIntent("OP-1", "EVT-1", TerminalOutcome.CREATED_INCIDENT, DecisionType.CREATE_NEW, "POLICY", "1.0", CorrelationFamily.ATTACK_SOURCE, DecisionReasonCode.NO_COMPATIBLE_CANDIDATE, "INC-1", None, AnchorStrength.STRONG, AnchorTransition.NONE, NOW)


def test_enter_pending_cannot_create_intent_and_invalid_state_fails_closed():
    with pytest.raises(StateDomainValidationError) as error:
        terminal_outcome_for_decision(DecisionType.ENTER_PENDING)
    assert error.value.code is StateDomainErrorCode.MUTATION_INTENT_CONFLICT
    with pytest.raises(StateDomainValidationError):
        CorrelationMutationIntent.from_decision(operation_id="OP-1", event_id="EVT-1", decision=_decision(DecisionType.ENTER_PENDING), created_at=NOW)
