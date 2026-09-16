from datetime import datetime, timedelta, timezone

import pytest

from alert_correlation.contracts import AnchorStrength, AnchorTransition, CorrelationFamily, DecisionReasonCode, DecisionType
from alert_correlation.policy import DEFAULT_POLICY_REGISTRY
from alert_correlation.state.contracts import CorrelationMutationIntent, TerminalOutcome
from shadow_management.contracts import ShadowDomainValidationError, ShadowMutationRequest, receipt_semantic_identity, validate_legal_shadow_mutation


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


def _shadow_intent(event_id: str = "EVT-1") -> CorrelationMutationIntent:
    return CorrelationMutationIntent("OP-1", event_id, TerminalOutcome.SHADOWED, DecisionType.ROUTE_SHADOW, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY, None, None, None, AnchorTransition.NONE, NOW)


def _request(intent: CorrelationMutationIntent | None = None, event: dict[str, object] | None = None, now: datetime = NOW) -> ShadowMutationRequest:
    return ShadowMutationRequest(intent or _shadow_intent(), event or {"event_id": "EVT-1", "event_type": "general_log_anomaly", "ignored": {"full": "event is not projected"}}, now)


def test_legal_real_route_shadow_intent_and_mapping_projection() -> None:
    request = _request()
    validate_legal_shadow_mutation(request, DEFAULT_POLICY_REGISTRY)
    assert request.event_projection == ("EVT-1", "general_log_anomaly")


def test_event_id_mismatch_and_unregistered_or_non_unknown_policy_fail_closed() -> None:
    with pytest.raises(ShadowDomainValidationError):
        validate_legal_shadow_mutation(_request(event={"event_id": "OTHER", "event_type": "general_log_anomaly"}), DEFAULT_POLICY_REGISTRY)
    with pytest.raises(ShadowDomainValidationError):
        validate_legal_shadow_mutation(_request(event={"event_id": "EVT-1", "event_type": "unregistered"}), DEFAULT_POLICY_REGISTRY)
    non_unknown = CorrelationMutationIntent("OP-2", "EVT-1", TerminalOutcome.SHADOWED, DecisionType.ROUTE_SHADOW, "POLICY-BRUTE-FORCE-DETECTED", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY, None, None, None, AnchorTransition.NONE, NOW)
    with pytest.raises(ShadowDomainValidationError):
        validate_legal_shadow_mutation(_request(non_unknown, {"event_id": "EVT-1", "event_type": "brute_force_detected"}), DEFAULT_POLICY_REGISTRY)


@pytest.mark.parametrize(
    ("decision", "outcome", "family", "reason", "target", "fingerprint", "anchor", "transition"),
    [
        (DecisionType.CREATE_NEW, TerminalOutcome.CREATED_INCIDENT, CorrelationFamily.UNKNOWN, DecisionReasonCode.NO_COMPATIBLE_CANDIDATE, None, None, AnchorStrength.STRONG, AnchorTransition.NONE),
        (DecisionType.ATTACH_EXISTING, TerminalOutcome.ATTACHED_TO_INCIDENT, CorrelationFamily.UNKNOWN, DecisionReasonCode.UNIQUE_COMPATIBLE_CANDIDATE, "INC-1", None, None, AnchorTransition.NONE),
    ],
)
def test_create_and_attach_are_wrong_domain(
    decision: DecisionType, outcome: TerminalOutcome, family: CorrelationFamily, reason: DecisionReasonCode, target: str | None, fingerprint: object, anchor: AnchorStrength | None, transition: AnchorTransition,
) -> None:
    intent = CorrelationMutationIntent("OP-wrong", "EVT-1", outcome, decision, "POLICY-GENERAL-LOG-ANOMALY", "1.0", family, reason, target, fingerprint, anchor, transition, NOW)  # type: ignore[arg-type]
    with pytest.raises(ShadowDomainValidationError) as error:
        validate_legal_shadow_mutation(_request(intent), DEFAULT_POLICY_REGISTRY)
    assert error.value.code.value == "INVALID_SHADOW_MUTATION"
    assert error.value.retry_disposition.value == "NON_RETRYABLE"


def test_receipt_identity_covers_real_intent_and_event_projection_but_excludes_now() -> None:
    original = _request(now=NOW)
    retry = _request(now=NOW + timedelta(minutes=5))
    assert receipt_semantic_identity(original) == receipt_semantic_identity(retry)
    changed_event_type = _request(event={"event_id": "EVT-1", "event_type": "general_metrics_anomaly"})
    assert receipt_semantic_identity(original) != receipt_semantic_identity(changed_event_type)
    changed_intent = CorrelationMutationIntent("OP-1", "EVT-1", TerminalOutcome.SHADOWED, DecisionType.ROUTE_SHADOW, "POLICY-GENERAL-METRICS-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY, None, None, None, AnchorTransition.NONE, NOW)
    assert receipt_semantic_identity(original) != receipt_semantic_identity(_request(changed_intent, {"event_id": "EVT-1", "event_type": "general_metrics_anomaly"}))


def test_contract_reuses_real_upstream_types_without_parallel_enums_or_event_dto() -> None:
    source = open("src/shadow_management/contracts.py", encoding="utf-8").read()
    assert "class CorrelationMutationIntent" not in source
    assert "class TerminalOutcome" not in source
    assert "class RetryDisposition" not in source
    assert "class ShadowEvent" not in source
