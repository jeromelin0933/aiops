import copy
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from src.alert_correlation import (
    DEFAULT_POLICY_REGISTRY,
    POC_V1_POLICIES,
    AlertCorrelationPolicyEngine,
    AnchorStrength,
    AnchorTransition,
    CorrelationDecision,
    CorrelationEngineConfig,
    CorrelationErrorCode,
    CorrelationEvaluationContext,
    CorrelationEvaluationError,
    CorrelationEvaluationFailure,
    CorrelationEvaluationSuccess,
    CorrelationFamily,
    CorrelationPolicy,
    DecisionReasonCode,
    DecisionType,
    EvaluationPhase,
    EvidenceClass,
    FieldPathIdentityExtractor,
    IdentityExtractionError,
    IdentityExtractor,
    IncidentCorrelationView,
    NormalizedFingerprint,
    PolicyDefinitionError,
    PolicyRegistry,
)
from src.event_detection.event.builder import EventBuilder
from src.event_detection.model.predictor import PredictionResult
from src.event_detection.model.schema import WindowSummary


def _strong_policy(
    *,
    policy_id="POLICY-TEST",
    policy_version="1.0",
    event_type="test_event",
    is_current=True,
):
    return CorrelationPolicy(
        policy_id=policy_id,
        policy_version=policy_version,
        event_type=event_type,
        evidence_class=EvidenceClass.STRONG,
        correlation_family=CorrelationFamily.ATTACK_SOURCE,
        identity_extractor=FieldPathIdentityExtractor("source_ip", ("source_ip",)),
        is_current=is_current,
    )


def test_enum_closed_sets_match_spec_006():
    assert {value.value for value in EvidenceClass} == {
        "STRONG", "KNOWN_WEAK", "UNKNOWN",
    }
    assert {value.value for value in CorrelationFamily} == {
        "ATTACK_SOURCE", "CROSS_SERVICE_LATENCY", "MEMORY_OOM",
        "EXTERNAL_DEPENDENCY", "DOWNSTREAM_CASCADE", "RATE_LIMIT", "UNKNOWN",
    }
    assert {value.value for value in AnchorStrength} == {"STRONG", "WEAK"}
    assert {value.value for value in AnchorTransition} == {
        "NONE", "WEAK_TO_STRONG",
    }
    assert {value.value for value in EvaluationPhase} == {
        "INITIAL", "PENDING_RECHECK", "PENDING_EXPIRED",
    }
    assert {value.value for value in DecisionType} == {
        "ATTACH_EXISTING", "CREATE_NEW", "ENTER_PENDING", "ROUTE_SHADOW",
    }
    assert {value.value for value in DecisionReasonCode} == {
        "EXACT_STRONG_IDENTITY_MATCH", "UNIQUE_COMPATIBLE_CANDIDATE",
        "WEAK_TO_STRONG_PROMOTION", "NO_COMPATIBLE_CANDIDATE",
        "MULTIPLE_COMPATIBLE_CANDIDATES", "PENDING_EXPIRED_UNRESOLVED",
        "INSUFFICIENT_OPERATIONAL_IDENTITY",
    }
    assert {value.value for value in CorrelationErrorCode} == {
        "INVALID_EVENT_ENVELOPE", "POLICY_NOT_REGISTERED",
        "POLICY_VERSION_UNAVAILABLE", "MISSING_REQUIRED_IDENTITY",
        "INVALID_IDENTITY_VALUE", "INVALID_INCIDENT_VIEW",
        "INCONSISTENT_CORRELATION_CONTEXT",
    }


def test_fingerprint_is_structured_canonical_and_order_independent():
    first = NormalizedFingerprint(
        "example_event", (("service_name", "payments"), ("region", "tw"))
    )
    second = NormalizedFingerprint.from_mapping(
        "example_event", {"region": "tw", "service_name": "payments"}
    )

    assert first == second
    assert first.identity == (("region", "tw"), ("service_name", "payments"))
    assert dict(first.as_mapping()) == {
        "region": "tw", "service_name": "payments",
    }
    with pytest.raises(TypeError):
        first.as_mapping()["region"] = "us"


@pytest.mark.parametrize(
    "identity,error_type",
    [
        ((), ValueError),
        (("not-a-pair",), TypeError),
        ((("service", "a"), ("service", "b")), ValueError),
        ((("", "value"),), ValueError),
        ((("service", ""),), ValueError),
    ],
)
def test_fingerprint_rejects_malformed_identity(identity, error_type):
    with pytest.raises(error_type):
        NormalizedFingerprint("event", identity)


def test_logical_contracts_and_results_are_frozen_and_mutually_exclusive():
    fingerprint = NormalizedFingerprint("event", (("id", "value"),))
    view = IncidentCorrelationView(
        incident_id="INC-1",
        status="OPEN",
        last_correlated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        correlation_family=CorrelationFamily.ATTACK_SOURCE,
        anchor_strength=AnchorStrength.STRONG,
        normalized_fingerprint=fingerprint,
        anchor_event_type="event",
    )
    context = CorrelationEvaluationContext(EvaluationPhase.INITIAL)
    decision = CorrelationDecision(
        decision_type=DecisionType.CREATE_NEW,
        policy_id="POLICY-TEST",
        policy_version="1.0",
        correlation_family=CorrelationFamily.ATTACK_SOURCE,
        reason_code=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
        normalized_fingerprint=fingerprint,
        anchor_strength=AnchorStrength.STRONG,
        diagnostics=(("candidate_count", 0),),
    )
    error = CorrelationEvaluationError(
        CorrelationErrorCode.INVALID_EVENT_ENVELOPE,
        event_id="EVT-1",
        event_type="event",
        message="invalid envelope",
    )
    success = CorrelationEvaluationSuccess(decision)
    failure = CorrelationEvaluationFailure(error)

    assert success.decision is decision
    assert not hasattr(success, "error")
    assert failure.error is error
    assert not hasattr(failure, "decision")
    for instance, field, value in (
        (view, "incident_id", "INC-2"),
        (context, "policy_id", "POLICY-OTHER"),
        (decision, "target_incident_id", "INC-1"),
        (error, "message", "changed"),
        (success, "decision", decision),
        (failure, "error", error),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(instance, field, value)


def test_decision_diagnostics_are_canonical_and_scalar_only():
    decision = CorrelationDecision(
        DecisionType.ENTER_PENDING,
        "POLICY-TEST",
        "1.0",
        CorrelationFamily.ATTACK_SOURCE,
        DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES,
        diagnostics=(("tier", "strong"), ("candidate_count", 2)),
    )
    assert decision.diagnostics == (("candidate_count", 2), ("tier", "strong"))
    with pytest.raises(TypeError):
        CorrelationDecision(
            DecisionType.ENTER_PENDING,
            "POLICY-TEST",
            "1.0",
            CorrelationFamily.ATTACK_SOURCE,
            DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES,
            diagnostics=(("candidate_ids", ["INC-1"]),),
        )


def test_engine_config_defaults_to_120_seconds_and_engine_is_immutable():
    config = CorrelationEngineConfig()
    engine = AlertCorrelationPolicyEngine(config=config)

    assert config.correlation_window_seconds == 120.0
    assert engine.config is config
    assert engine.registry is DEFAULT_POLICY_REGISTRY
    assert callable(engine.evaluate)
    with pytest.raises(FrozenInstanceError):
        engine.config = CorrelationEngineConfig(60)


@pytest.mark.parametrize("value", [1, 0.5, 120, 300.25])
def test_engine_config_accepts_positive_finite_numbers(value):
    assert CorrelationEngineConfig(value).correlation_window_seconds == float(value)


@pytest.mark.parametrize(
    "value,error_type",
    [
        (True, TypeError),
        (False, TypeError),
        ("120", TypeError),
        (None, TypeError),
        (0, ValueError),
        (-1, ValueError),
        (float("inf"), ValueError),
        (float("-inf"), ValueError),
        (float("nan"), ValueError),
    ],
)
def test_engine_config_rejects_invalid_values_at_bootstrap(value, error_type):
    with pytest.raises(error_type):
        CorrelationEngineConfig(value)


POLICY_MATRIX = {
    "brute_force_detected": (
        "POLICY-BRUTE-FORCE-DETECTED", EvidenceClass.STRONG,
        CorrelationFamily.ATTACK_SOURCE, ("source_ip",),
    ),
    "cross_service_failure": (
        "POLICY-CROSS-SERVICE-FAILURE", EvidenceClass.STRONG,
        CorrelationFamily.CROSS_SERVICE_LATENCY, ("trace_id",),
    ),
    "high_latency_detected": (
        "POLICY-HIGH-LATENCY-DETECTED", EvidenceClass.KNOWN_WEAK,
        CorrelationFamily.CROSS_SERVICE_LATENCY, None,
    ),
    "oom_crash_detected": (
        "POLICY-OOM-CRASH-DETECTED", EvidenceClass.STRONG,
        CorrelationFamily.MEMORY_OOM, ("service_name",),
    ),
    "high_memory_detected": (
        "POLICY-HIGH-MEMORY-DETECTED", EvidenceClass.KNOWN_WEAK,
        CorrelationFamily.MEMORY_OOM, None,
    ),
    "external_dependency_failure": (
        "POLICY-EXTERNAL-DEPENDENCY-FAILURE", EvidenceClass.STRONG,
        CorrelationFamily.EXTERNAL_DEPENDENCY, ("external_service",),
    ),
    "downstream_cascade_failure": (
        "POLICY-DOWNSTREAM-CASCADE-FAILURE", EvidenceClass.STRONG,
        CorrelationFamily.DOWNSTREAM_CASCADE, ("downstream_service",),
    ),
    "rate_limit_storm": (
        "POLICY-RATE-LIMIT-STORM", EvidenceClass.STRONG,
        CorrelationFamily.RATE_LIMIT, ("triggered_features", "target_service"),
    ),
    "request_spike_detected": (
        "POLICY-REQUEST-SPIKE-DETECTED", EvidenceClass.KNOWN_WEAK,
        CorrelationFamily.RATE_LIMIT, None,
    ),
    "general_log_anomaly": (
        "POLICY-GENERAL-LOG-ANOMALY", EvidenceClass.UNKNOWN,
        CorrelationFamily.UNKNOWN, None,
    ),
    "general_metrics_anomaly": (
        "POLICY-GENERAL-METRICS-ANOMALY", EvidenceClass.UNKNOWN,
        CorrelationFamily.UNKNOWN, None,
    ),
}


@pytest.mark.parametrize(
    "event_type,expected",
    POLICY_MATRIX.items(),
)
def test_poc_v1_policy_matrix(event_type, expected):
    policy_id, evidence_class, family, field_path = expected
    policy = DEFAULT_POLICY_REGISTRY.resolve_current(event_type)

    assert policy is not None
    assert policy.policy_id == policy_id
    assert policy.policy_version == "1.0"
    assert policy.evidence_class is evidence_class
    assert policy.correlation_family is family
    if field_path is None:
        assert policy.identity_extractor is None
    else:
        assert isinstance(policy.identity_extractor, FieldPathIdentityExtractor)
        assert policy.identity_extractor.field_path == field_path


def test_registry_current_and_exact_historical_lookup_are_distinct():
    historical = _strong_policy(policy_version="1.0", is_current=False)
    current = _strong_policy(policy_version="2.0", is_current=True)
    registry = PolicyRegistry([current, historical])

    assert registry.resolve_current("test_event") is current
    assert registry.resolve_exact("POLICY-TEST", "1.0") is historical
    assert registry.resolve_exact("POLICY-TEST", "2.0") is current
    assert registry.resolve_exact("POLICY-TEST", "9.9") is None
    assert registry.resolve_current("unregistered") is None


def test_registry_lookup_and_exposed_policy_order_are_input_order_independent():
    forward = PolicyRegistry(POC_V1_POLICIES)
    reverse = PolicyRegistry(reversed(POC_V1_POLICIES))

    assert forward.policies == reverse.policies
    for event_type in POLICY_MATRIX:
        assert forward.resolve_current(event_type) == reverse.resolve_current(event_type)
    for policy in POC_V1_POLICIES:
        assert forward.resolve_exact(*policy.reference) == reverse.resolve_exact(
            *policy.reference
        )


def test_registry_and_policy_objects_are_immutable():
    policy = DEFAULT_POLICY_REGISTRY.resolve_current("brute_force_detected")
    assert policy is not None
    with pytest.raises(FrozenInstanceError):
        policy.event_type = "changed"
    with pytest.raises(FrozenInstanceError):
        DEFAULT_POLICY_REGISTRY._policies = ()
    with pytest.raises(TypeError):
        DEFAULT_POLICY_REGISTRY.current_by_event_type["new"] = policy


def test_top_level_extractor_trims_only_and_does_not_mutate_runtime_event():
    policy = DEFAULT_POLICY_REGISTRY.resolve_current("oom_crash_detected")
    event = {"service_name": "  Payment-API  "}

    fingerprint = policy.fingerprint_for(event)

    assert fingerprint.identity == (("service_name", "Payment-API"),)
    assert event == {"service_name": "  Payment-API  "}


def test_s6_extractor_reads_nested_runtime_event_path():
    policy = DEFAULT_POLICY_REGISTRY.resolve_current("rate_limit_storm")
    event = {
        "event_type": "rate_limit_storm",
        "service_name": "gateway-api",
        "triggered_features": {"target_service": "  SMS-Gateway  "},
    }

    fingerprint = policy.fingerprint_for(event)

    assert fingerprint == NormalizedFingerprint(
        "rate_limit_storm", (("target_service", "SMS-Gateway"),)
    )
    assert "target_service" not in {
        key for key in event if key != "triggered_features"
    }


@pytest.mark.parametrize(
    "event_type,event,identity_field,identity_value",
    [
        (
            "brute_force_detected",
            {"source_ip": "192.0.2.10"},
            "source_ip",
            "192.0.2.10",
        ),
        (
            "cross_service_failure",
            {"trace_id": "trace-S2"},
            "trace_id",
            "trace-S2",
        ),
        (
            "oom_crash_detected",
            {"service_name": "payment-api"},
            "service_name",
            "payment-api",
        ),
        (
            "external_dependency_failure",
            {"external_service": "payment-gateway"},
            "external_service",
            "payment-gateway",
        ),
        (
            "downstream_cascade_failure",
            {"downstream_service": "inventory-db"},
            "downstream_service",
            "inventory-db",
        ),
        (
            "rate_limit_storm",
            {"triggered_features": {"target_service": "sms-gateway"}},
            "target_service",
            "sms-gateway",
        ),
    ],
)
def test_s1_through_s6_strong_fingerprint_extraction(
    event_type,
    event,
    identity_field,
    identity_value,
):
    policy = DEFAULT_POLICY_REGISTRY.resolve_current(event_type)
    assert policy is not None

    fingerprint = policy.fingerprint_for(event)

    assert fingerprint == NormalizedFingerprint(
        event_type,
        ((identity_field, identity_value),),
    )


def test_s6_never_falls_back_to_top_level_target_service():
    event = {
        "target_service": "unsafe-top-level-value",
        "triggered_features": {},
    }
    policy = DEFAULT_POLICY_REGISTRY.resolve_current("rate_limit_storm")
    assert policy is not None

    with pytest.raises(IdentityExtractionError) as captured:
        policy.fingerprint_for(event)

    assert captured.value.error_code is CorrelationErrorCode.MISSING_REQUIRED_IDENTITY
    assert captured.value.field_path == "triggered_features.target_service"


@pytest.mark.parametrize(
    "event,field_path",
    [
        ({}, "triggered_features"),
        ({"triggered_features": None}, "triggered_features"),
        ({"triggered_features": {}}, "triggered_features.target_service"),
        (
            {"triggered_features": {"target_service": None}},
            "triggered_features.target_service",
        ),
    ],
)
def test_extractor_reports_missing_path_or_null_as_missing_identity(
    event, field_path
):
    policy = DEFAULT_POLICY_REGISTRY.resolve_current("rate_limit_storm")
    with pytest.raises(IdentityExtractionError) as captured:
        policy.fingerprint_for(event)
    assert captured.value.error_code is CorrelationErrorCode.MISSING_REQUIRED_IDENTITY
    assert captured.value.field_path == field_path


@pytest.mark.parametrize(
    "event,field_path",
    [
        ({"triggered_features": []}, "triggered_features"),
        (
            {"triggered_features": {"target_service": 123}},
            "triggered_features.target_service",
        ),
        (
            {"triggered_features": {"target_service": "   "}},
            "triggered_features.target_service",
        ),
    ],
)
def test_extractor_reports_wrong_type_or_empty_as_invalid_identity(
    event, field_path
):
    policy = DEFAULT_POLICY_REGISTRY.resolve_current("rate_limit_storm")
    with pytest.raises(IdentityExtractionError) as captured:
        policy.fingerprint_for(event)
    assert captured.value.error_code is CorrelationErrorCode.INVALID_IDENTITY_VALUE
    assert captured.value.field_path == field_path


def test_extractor_does_not_guess_aliases_or_lowercase_values():
    policy = DEFAULT_POLICY_REGISTRY.resolve_current("brute_force_detected")
    with pytest.raises(IdentityExtractionError) as captured:
        policy.fingerprint_for({"attacker_ip": "192.0.2.10"})
    assert captured.value.error_code is CorrelationErrorCode.MISSING_REQUIRED_IDENTITY

    fingerprint = policy.fingerprint_for({"source_ip": " Example-IP "})
    assert fingerprint.identity == (("source_ip", "Example-IP"),)


def test_registry_bootstrap_rejects_duplicate_current_event_type():
    first = _strong_policy(policy_id="POLICY-A")
    second = _strong_policy(policy_id="POLICY-B")
    with pytest.raises(PolicyDefinitionError, match="duplicate current event_type"):
        PolicyRegistry([first, second])


def test_registry_bootstrap_rejects_duplicate_exact_policy_reference():
    first = _strong_policy(event_type="first", is_current=False)
    second = _strong_policy(event_type="second", is_current=False)
    with pytest.raises(PolicyDefinitionError, match="duplicate policy_id"):
        PolicyRegistry([first, second])


def test_registry_bootstrap_rejects_strong_without_extractor():
    with pytest.raises(PolicyDefinitionError, match="requires an identity extractor"):
        CorrelationPolicy(
            "POLICY-INVALID", "1.0", "event", EvidenceClass.STRONG,
            CorrelationFamily.ATTACK_SOURCE,
        )


@pytest.mark.parametrize("evidence_class", [EvidenceClass.KNOWN_WEAK, EvidenceClass.UNKNOWN])
def test_registry_bootstrap_rejects_weak_or_unknown_extractor(evidence_class):
    family = (
        CorrelationFamily.UNKNOWN
        if evidence_class is EvidenceClass.UNKNOWN
        else CorrelationFamily.ATTACK_SOURCE
    )
    with pytest.raises(PolicyDefinitionError, match="must not define"):
        CorrelationPolicy(
            "POLICY-INVALID", "1.0", "event", evidence_class, family,
            FieldPathIdentityExtractor("source_ip", ("source_ip",)),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"policy_id": ""},
        {"policy_version": " 1.0"},
        {"event_type": " "},
        {"evidence_class": "STRONG"},
        {"correlation_family": "ATTACK_SOURCE"},
        {"is_current": 1},
    ],
)
def test_registry_bootstrap_rejects_malformed_policy_definitions(kwargs):
    values = {
        "policy_id": "POLICY-TEST",
        "policy_version": "1.0",
        "event_type": "event",
        "evidence_class": EvidenceClass.STRONG,
        "correlation_family": CorrelationFamily.ATTACK_SOURCE,
        "identity_extractor": FieldPathIdentityExtractor(
            "source_ip", ("source_ip",)
        ),
        "is_current": True,
    }
    values.update(kwargs)
    with pytest.raises(PolicyDefinitionError):
        CorrelationPolicy(**values)


def test_registry_bootstrap_rejects_unknown_family_mismatch_and_non_policy_items():
    with pytest.raises(PolicyDefinitionError, match="must be paired"):
        CorrelationPolicy(
            "POLICY-INVALID", "1.0", "event", EvidenceClass.UNKNOWN,
            CorrelationFamily.ATTACK_SOURCE,
        )
    with pytest.raises(PolicyDefinitionError, match="CorrelationPolicy"):
        PolicyRegistry([object()])
    with pytest.raises(PolicyDefinitionError, match="at least one"):
        PolicyRegistry([])


_EVENT_TIME = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
_UNSET = object()


def _runtime_event(
    event_type="high_memory_detected",
    *,
    detected_at="2026-08-30T12:00:00Z",
    **fields,
):
    event = {
        "event_id": "EVT-PHASE-3",
        "event_type": event_type,
        "detected_at": detected_at,
    }
    event.update(fields)
    return event


def _incident_view(
    incident_id="INC-1",
    *,
    status="OPEN",
    last_correlated_at=_EVENT_TIME,
    family=CorrelationFamily.MEMORY_OOM,
    strength=AnchorStrength.STRONG,
    fingerprint=_UNSET,
    anchor_event_type=_UNSET,
):
    if fingerprint is _UNSET:
        fingerprint = (
            NormalizedFingerprint(
                "oom_crash_detected", (("service_name", "payments"),)
            )
            if strength is AnchorStrength.STRONG
            else None
        )
    if anchor_event_type is _UNSET:
        anchor_event_type = (
            fingerprint.event_type
            if isinstance(fingerprint, NormalizedFingerprint)
            else None
        )
    return IncidentCorrelationView(
        incident_id=incident_id,
        status=status,
        last_correlated_at=last_correlated_at,
        correlation_family=family,
        anchor_strength=strength,
        normalized_fingerprint=fingerprint,
        anchor_event_type=anchor_event_type,
    )


def _prepare(event, views=(), context=None, *, engine=None):
    resolved_engine = engine or AlertCorrelationPolicyEngine()
    resolved_context = context or CorrelationEvaluationContext(
        EvaluationPhase.INITIAL
    )
    return resolved_engine._prepare_evaluation(
        event,
        views,
        resolved_context,
    )


def _assert_foundation(result):
    assert not isinstance(result, CorrelationEvaluationFailure)
    return result


@pytest.mark.parametrize(
    "event,field_path",
    [
        (None, "event"),
        ({}, "event_id"),
        (_runtime_event() | {"event_id": 123}, "event_id"),
        (_runtime_event() | {"event_type": " "}, "event_type"),
        (_runtime_event(detected_at=None), "detected_at"),
        (_runtime_event(detected_at="not-a-timestamp"), "detected_at"),
        (_runtime_event(detected_at="2026-08-30T12:00:00"), "detected_at"),
        (_runtime_event(detected_at="2026-08-30T13:00:00+01:00"), "detected_at"),
    ],
)
def test_event_envelope_failures_are_structured(event, field_path):
    result = _prepare(event)

    assert isinstance(result, CorrelationEvaluationFailure)
    assert result.error.error_code is CorrelationErrorCode.INVALID_EVENT_ENVELOPE
    assert result.error.field_path == field_path


def test_event_envelope_parses_production_iso_timestamp_without_full_schema_recheck():
    result = _assert_foundation(_prepare(_runtime_event()))

    assert result.event_id == "EVT-PHASE-3"
    assert result.event_type == "high_memory_detected"
    assert result.detected_at == _EVENT_TIME


@pytest.mark.parametrize(
    "context,field_path",
    [
        (
            CorrelationEvaluationContext(EvaluationPhase.PENDING_RECHECK),
            "policy_id,policy_version",
        ),
        (
            CorrelationEvaluationContext(
                EvaluationPhase.PENDING_EXPIRED,
                policy_id="POLICY-HIGH-MEMORY-DETECTED",
            ),
            "policy_id,policy_version",
        ),
        (
            CorrelationEvaluationContext(
                EvaluationPhase.INITIAL,
                policy_id="POLICY-HIGH-MEMORY-DETECTED",
            ),
            "policy_version",
        ),
        (
            CorrelationEvaluationContext("INITIAL"),
            "evaluation_phase",
        ),
    ],
)
def test_inconsistent_correlation_context_returns_structured_failure(
    context,
    field_path,
):
    result = _prepare(_runtime_event(), context=context)

    assert isinstance(result, CorrelationEvaluationFailure)
    assert (
        result.error.error_code
        is CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT
    )
    assert result.error.field_path == field_path


def test_initial_optional_policy_reference_must_match_current_policy():
    matching = CorrelationEvaluationContext(
        EvaluationPhase.INITIAL,
        "POLICY-HIGH-MEMORY-DETECTED",
        "1.0",
    )
    mismatching = CorrelationEvaluationContext(
        EvaluationPhase.INITIAL,
        "POLICY-REQUEST-SPIKE-DETECTED",
        "1.0",
    )

    assert not isinstance(
        _prepare(_runtime_event(), context=matching),
        CorrelationEvaluationFailure,
    )
    result = _prepare(_runtime_event(), context=mismatching)
    assert isinstance(result, CorrelationEvaluationFailure)
    assert (
        result.error.error_code
        is CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT
    )


@pytest.mark.parametrize(
    "phase",
    [EvaluationPhase.PENDING_RECHECK, EvaluationPhase.PENDING_EXPIRED],
)
def test_pending_phases_resolve_exact_historical_policy(phase):
    historical = _strong_policy(policy_version="1.0", is_current=False)
    current = _strong_policy(policy_version="2.0", is_current=True)
    engine = AlertCorrelationPolicyEngine(
        registry=PolicyRegistry([current, historical])
    )
    context = CorrelationEvaluationContext(phase, "POLICY-TEST", "1.0")

    result = _assert_foundation(
        _prepare(
            _runtime_event("test_event", source_ip="192.0.2.1"),
            context=context,
            engine=engine,
        )
    )

    assert result.policy is historical


def test_pending_unavailable_exact_policy_does_not_fallback_to_current():
    context = CorrelationEvaluationContext(
        EvaluationPhase.PENDING_RECHECK,
        "POLICY-HIGH-MEMORY-DETECTED",
        "9.9",
    )

    result = _prepare(_runtime_event(), context=context)

    assert isinstance(result, CorrelationEvaluationFailure)
    assert result.error.error_code is CorrelationErrorCode.POLICY_VERSION_UNAVAILABLE
    assert result.error.policy_id == "POLICY-HIGH-MEMORY-DETECTED"
    assert result.error.policy_version == "9.9"


def test_pending_exact_policy_must_match_event_type():
    context = CorrelationEvaluationContext(
        EvaluationPhase.PENDING_RECHECK,
        "POLICY-REQUEST-SPIKE-DETECTED",
        "1.0",
    )

    result = _prepare(_runtime_event(), context=context)

    assert isinstance(result, CorrelationEvaluationFailure)
    assert (
        result.error.error_code
        is CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT
    )


def test_unregistered_event_type_is_policy_failure_not_shadow():
    result = _prepare(_runtime_event("not_registered"))

    assert isinstance(result, CorrelationEvaluationFailure)
    assert result.error.error_code is CorrelationErrorCode.POLICY_NOT_REGISTERED


def test_strong_identity_error_is_converted_to_structured_failure():
    result = _prepare(_runtime_event("oom_crash_detected"))

    assert isinstance(result, CorrelationEvaluationFailure)
    assert result.error.error_code is CorrelationErrorCode.MISSING_REQUIRED_IDENTITY
    assert result.error.field_path == "service_name"


@pytest.mark.parametrize(
    "status,expected_count",
    [
        ("OPEN", 1),
        ("ASSIGNED", 1),
        ("IN_PROGRESS", 1),
        ("AWAITING_REVIEW", 0),
        ("CLOSED", 0),
    ],
)
def test_lifecycle_eligibility_matrix(status, expected_count):
    view = _incident_view(status=status)

    result = _assert_foundation(_prepare(_runtime_event(), [view]))

    assert len(result.compatible_candidates) == expected_count


@pytest.mark.parametrize(
    "delta_seconds,expected_count",
    [
        (0, 1),
        (120, 1),
        (120.000001, 0),
        (-1, 0),
    ],
)
def test_forward_only_correlation_window_boundaries(delta_seconds, expected_count):
    last_correlated_at = _EVENT_TIME - timedelta(seconds=delta_seconds)
    view = _incident_view(last_correlated_at=last_correlated_at)

    result = _assert_foundation(_prepare(_runtime_event(), [view]))

    assert len(result.compatible_candidates) == expected_count


def test_negative_delta_is_ineligible_not_a_failure():
    future_incident = _incident_view(
        last_correlated_at=_EVENT_TIME + timedelta(seconds=1)
    )

    result = _assert_foundation(_prepare(_runtime_event(), [future_incident]))

    assert result.compatible_candidates == ()


def test_alternate_correlation_window_is_used_by_candidate_gate():
    engine = AlertCorrelationPolicyEngine(CorrelationEngineConfig(30))
    view = _incident_view(last_correlated_at=_EVENT_TIME - timedelta(seconds=31))

    result = _assert_foundation(
        _prepare(_runtime_event(), [view], engine=engine)
    )

    assert result.compatible_candidates == ()


@pytest.mark.parametrize("sequence_type", [list, tuple], ids=["list", "tuple"])
def test_incident_views_accepts_normal_sequence_inputs(sequence_type):
    views = sequence_type([_incident_view()])

    result = AlertCorrelationPolicyEngine().evaluate(
        _runtime_event(),
        views,
        CorrelationEvaluationContext(EvaluationPhase.INITIAL),
    )

    assert isinstance(result, CorrelationEvaluationSuccess)
    assert result.decision.decision_type is DecisionType.ATTACH_EXISTING
    assert result.decision.target_incident_id == "INC-1"


def test_incident_views_rejects_iterable_that_is_not_a_sequence():
    incident_views = iter((_incident_view(),))

    result = AlertCorrelationPolicyEngine().evaluate(
        _runtime_event(),
        incident_views,
        CorrelationEvaluationContext(EvaluationPhase.INITIAL),
    )

    assert isinstance(result, CorrelationEvaluationFailure)
    assert result.error.error_code is CorrelationErrorCode.INVALID_INCIDENT_VIEW
    assert result.error.field_path == "incident_views"


@pytest.mark.parametrize(
    "error_type,message",
    [
        (TypeError, "iterator programming bug"),
        (RuntimeError, "unexpected iterator runtime bug"),
    ],
)
def test_sequence_iteration_programming_failure_propagates(error_type, message):
    class BuggySequence(Sequence):
        def __len__(self):
            return 1

        def __getitem__(self, index):
            raise AssertionError("materialization must use the buggy iterator")

        def __iter__(self):
            raise error_type(message)

    with pytest.raises(error_type, match=message):
        AlertCorrelationPolicyEngine().evaluate(
            _runtime_event(),
            BuggySequence(),
            CorrelationEvaluationContext(EvaluationPhase.INITIAL),
        )


def test_family_mismatch_never_becomes_candidate():
    view = _incident_view(family=CorrelationFamily.RATE_LIMIT)

    result = _assert_foundation(_prepare(_runtime_event(), [view]))

    assert result.compatible_candidates == ()


@pytest.mark.parametrize("status", ["AWAITING_REVIEW", "CLOSED"])
def test_correlation_closed_view_skips_malformed_stage_two(status):
    historical_corruption = _incident_view(
        status=status,
        family="CORRUPT",
        strength="CORRUPT",
        fingerprint="CORRUPT",
        anchor_event_type=123,
    )

    result = _assert_foundation(
        _prepare(_runtime_event(), [historical_corruption])
    )

    assert result.compatible_candidates == ()


def test_out_of_window_view_skips_malformed_stage_two():
    historical_corruption = _incident_view(
        last_correlated_at=_EVENT_TIME - timedelta(seconds=121),
        family="CORRUPT",
        strength="CORRUPT",
        fingerprint="CORRUPT",
        anchor_event_type=123,
    )

    result = _assert_foundation(
        _prepare(_runtime_event(), [historical_corruption])
    )

    assert result.compatible_candidates == ()


@pytest.mark.parametrize(
    "view,field_path",
    [
        (_incident_view(incident_id=""), "incident_id"),
        (_incident_view(status="RESOLVED"), "status"),
        (_incident_view(last_correlated_at="not-a-timestamp"), "last_correlated_at"),
        (_incident_view(family="MEMORY_OOM"), "correlation_family"),
        (_incident_view(strength="STRONG"), "anchor_strength"),
        (
            _incident_view(
                strength=AnchorStrength.STRONG,
                fingerprint=None,
                anchor_event_type="oom_crash_detected",
            ),
            "normalized_fingerprint",
        ),
        (
            _incident_view(
                strength=AnchorStrength.STRONG,
                anchor_event_type="wrong_event_type",
            ),
            "anchor_event_type",
        ),
        (
            _incident_view(
                strength=AnchorStrength.WEAK,
                fingerprint=NormalizedFingerprint(
                    "oom_crash_detected", (("service_name", "payments"),)
                ),
                anchor_event_type=None,
            ),
            "normalized_fingerprint",
        ),
        (
            _incident_view(
                strength=AnchorStrength.WEAK,
                fingerprint=None,
                anchor_event_type="high_memory_detected",
            ),
            "anchor_event_type",
        ),
    ],
)
def test_malformed_eligible_view_fails_closed(view, field_path):
    result = _prepare(_runtime_event(), [view])

    assert isinstance(result, CorrelationEvaluationFailure)
    assert result.error.error_code is CorrelationErrorCode.INVALID_INCIDENT_VIEW
    assert result.error.field_path == field_path


def test_candidate_input_order_does_not_change_foundation():
    views = [
        _incident_view("INC-3", strength=AnchorStrength.WEAK),
        _incident_view("INC-1"),
        _incident_view("INC-2", strength=AnchorStrength.WEAK),
    ]

    forward = _assert_foundation(_prepare(_runtime_event(), views))
    reverse = _assert_foundation(_prepare(_runtime_event(), list(reversed(views))))

    assert forward == reverse
    assert [candidate.incident_id for candidate in forward.compatible_candidates] == [
        "INC-1",
        "INC-2",
        "INC-3",
    ]


def test_malformed_view_failure_is_input_order_invariant():
    invalid_status = _incident_view("INC-2", status="RESOLVED")
    invalid_family = _incident_view("INC-1", family="MEMORY_OOM")

    forward = _prepare(_runtime_event(), [invalid_status, invalid_family])
    reverse = _prepare(_runtime_event(), [invalid_family, invalid_status])

    assert isinstance(forward, CorrelationEvaluationFailure)
    assert forward == reverse
    assert forward.error.incident_id == "INC-1"


def test_strong_tier_one_is_exact_fingerprint_set_and_suppresses_tier_two():
    exact = _incident_view("INC-EXACT")
    nonmatch = _incident_view(
        "INC-NONMATCH",
        fingerprint=NormalizedFingerprint(
            "oom_crash_detected", (("service_name", "orders"),)
        ),
    )
    weak = _incident_view("INC-WEAK", strength=AnchorStrength.WEAK)

    result = _assert_foundation(
        _prepare(
            _runtime_event("oom_crash_detected", service_name="payments"),
            [weak, nonmatch, exact],
        )
    )

    assert [candidate.incident_id for candidate in result.tier_1_candidates] == [
        "INC-EXACT"
    ]
    assert result.tier_2_candidates == ()
    assert result.compatible_candidates == result.tier_1_candidates


def test_strong_tier_one_ambiguity_does_not_fallback_to_weak_candidates():
    views = [
        _incident_view("INC-EXACT-2"),
        _incident_view("INC-WEAK", strength=AnchorStrength.WEAK),
        _incident_view("INC-EXACT-1"),
    ]

    result = _assert_foundation(
        _prepare(
            _runtime_event("oom_crash_detected", service_name="payments"),
            views,
        )
    )

    assert [candidate.incident_id for candidate in result.tier_1_candidates] == [
        "INC-EXACT-1",
        "INC-EXACT-2",
    ]
    assert result.tier_2_candidates == ()
    assert "INC-WEAK" not in {
        candidate.incident_id for candidate in result.compatible_candidates
    }


def test_strong_tier_two_collects_all_weak_standalone_candidates_only():
    nonmatch = _incident_view(
        "INC-STRONG-NONMATCH",
        fingerprint=NormalizedFingerprint(
            "oom_crash_detected", (("service_name", "orders"),)
        ),
    )
    weak_two = _incident_view("INC-WEAK-2", strength=AnchorStrength.WEAK)
    weak_one = _incident_view("INC-WEAK-1", strength=AnchorStrength.WEAK)

    result = _assert_foundation(
        _prepare(
            _runtime_event("oom_crash_detected", service_name="payments"),
            [weak_two, nonmatch, weak_one],
        )
    )

    assert result.tier_1_candidates == ()
    assert [candidate.incident_id for candidate in result.tier_2_candidates] == [
        "INC-WEAK-1",
        "INC-WEAK-2",
    ]
    assert result.compatible_candidates == result.tier_2_candidates


def test_known_weak_collects_strong_and_weak_candidates_without_priority():
    strong = _incident_view("INC-STRONG")
    weak = _incident_view("INC-WEAK", strength=AnchorStrength.WEAK)

    result = _assert_foundation(
        _prepare(_runtime_event(), [weak, strong])
    )

    assert [candidate.incident_id for candidate in result.compatible_candidates] == [
        "INC-STRONG",
        "INC-WEAK",
    ]
    assert {
        candidate.anchor_strength for candidate in result.compatible_candidates
    } == {AnchorStrength.STRONG, AnchorStrength.WEAK}
    assert result.tier_1_candidates == ()
    assert result.tier_2_candidates == ()


def _evaluation_context(event_type, phase=EvaluationPhase.INITIAL):
    if phase is EvaluationPhase.INITIAL:
        return CorrelationEvaluationContext(phase)
    policy = DEFAULT_POLICY_REGISTRY.resolve_current(event_type)
    assert policy is not None
    return CorrelationEvaluationContext(
        phase,
        policy.policy_id,
        policy.policy_version,
    )


def _evaluate_success(event, views=(), phase=EvaluationPhase.INITIAL, *, engine=None):
    resolved_engine = engine or AlertCorrelationPolicyEngine()
    result = resolved_engine.evaluate(
        event,
        views,
        _evaluation_context(event["event_type"], phase),
    )
    assert isinstance(result, CorrelationEvaluationSuccess)
    assert not hasattr(result, "error")
    return result.decision


def _strong_oom_event(service_name="payments"):
    return _runtime_event("oom_crash_detected", service_name=service_name)


@pytest.mark.parametrize(
    "event_type",
    ["general_log_anomaly", "general_metrics_anomaly"],
)
def test_registered_unknown_routes_shadow_without_candidate_matching(event_type):
    malformed_view_that_must_not_be_read = object()

    decision = _evaluate_success(
        _runtime_event(event_type),
        [malformed_view_that_must_not_be_read],
    )

    assert decision.decision_type is DecisionType.ROUTE_SHADOW
    assert decision.reason_code is DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY
    assert decision.correlation_family is CorrelationFamily.UNKNOWN
    assert decision.target_incident_id is None
    assert decision.normalized_fingerprint is None
    assert decision.anchor_strength is None
    assert decision.anchor_transition is AnchorTransition.NONE
    assert decision.diagnostics == ()


def test_unregistered_event_returns_failure_before_candidate_processing():
    result = AlertCorrelationPolicyEngine().evaluate(
        _runtime_event("unregistered_event"),
        [object()],
        CorrelationEvaluationContext(EvaluationPhase.INITIAL),
    )

    assert isinstance(result, CorrelationEvaluationFailure)
    assert not hasattr(result, "decision")
    assert result.error.error_code is CorrelationErrorCode.POLICY_NOT_REGISTERED


@pytest.mark.parametrize(
    "phase",
    [
        EvaluationPhase.INITIAL,
        EvaluationPhase.PENDING_RECHECK,
        EvaluationPhase.PENDING_EXPIRED,
    ],
)
def test_strong_unique_exact_candidate_attaches_in_every_phase(phase):
    exact = _incident_view("INC-EXACT")

    decision = _evaluate_success(_strong_oom_event(), [exact], phase)

    assert decision.decision_type is DecisionType.ATTACH_EXISTING
    assert decision.reason_code is DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH
    assert decision.target_incident_id == "INC-EXACT"
    assert decision.anchor_strength is AnchorStrength.STRONG
    assert decision.anchor_transition is AnchorTransition.NONE
    assert decision.normalized_fingerprint == NormalizedFingerprint(
        "oom_crash_detected", (("service_name", "payments"),)
    )
    assert decision.diagnostics == (("candidate_count", 1),)


@pytest.mark.parametrize(
    "phase",
    [
        EvaluationPhase.INITIAL,
        EvaluationPhase.PENDING_RECHECK,
        EvaluationPhase.PENDING_EXPIRED,
    ],
)
def test_strong_unique_weak_candidate_promotes_in_every_phase(phase):
    weak = _incident_view("INC-WEAK", strength=AnchorStrength.WEAK)

    decision = _evaluate_success(_strong_oom_event(), [weak], phase)

    assert decision.decision_type is DecisionType.ATTACH_EXISTING
    assert decision.reason_code is DecisionReasonCode.WEAK_TO_STRONG_PROMOTION
    assert decision.target_incident_id == "INC-WEAK"
    assert decision.anchor_strength is AnchorStrength.STRONG
    assert decision.anchor_transition is AnchorTransition.WEAK_TO_STRONG
    assert decision.normalized_fingerprint is not None


@pytest.mark.parametrize(
    "phase,candidate_count,expected_type,expected_reason,expected_strength",
    [
        (
            EvaluationPhase.INITIAL,
            0,
            DecisionType.CREATE_NEW,
            DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
            AnchorStrength.STRONG,
        ),
        (
            EvaluationPhase.PENDING_RECHECK,
            0,
            DecisionType.ENTER_PENDING,
            DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
            None,
        ),
        (
            EvaluationPhase.PENDING_EXPIRED,
            0,
            DecisionType.CREATE_NEW,
            DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
            AnchorStrength.STRONG,
        ),
        (
            EvaluationPhase.INITIAL,
            2,
            DecisionType.ENTER_PENDING,
            DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES,
            None,
        ),
        (
            EvaluationPhase.PENDING_RECHECK,
            2,
            DecisionType.ENTER_PENDING,
            DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES,
            None,
        ),
        (
            EvaluationPhase.PENDING_EXPIRED,
            2,
            DecisionType.CREATE_NEW,
            DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
            AnchorStrength.STRONG,
        ),
    ],
)
def test_strong_unresolved_phase_matrix(
    phase,
    candidate_count,
    expected_type,
    expected_reason,
    expected_strength,
):
    views = [_incident_view(f"INC-{index}") for index in range(candidate_count)]

    decision = _evaluate_success(_strong_oom_event(), views, phase)

    assert decision.decision_type is expected_type
    assert decision.reason_code is expected_reason
    assert decision.target_incident_id is None
    assert decision.anchor_strength is expected_strength
    assert decision.anchor_transition is AnchorTransition.NONE
    assert decision.normalized_fingerprint is not None
    assert decision.diagnostics == (("candidate_count", candidate_count),)


def test_strong_tier_one_ambiguity_decision_never_falls_back_to_weak():
    views = [
        _incident_view("INC-EXACT-2"),
        _incident_view("INC-WEAK", strength=AnchorStrength.WEAK),
        _incident_view("INC-EXACT-1"),
    ]

    decision = _evaluate_success(_strong_oom_event(), views)

    assert decision.decision_type is DecisionType.ENTER_PENDING
    assert decision.reason_code is DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES
    assert decision.diagnostics == (("candidate_count", 2),)


def test_multiple_weak_promotion_candidates_are_ambiguous():
    views = [
        _incident_view("INC-WEAK-2", strength=AnchorStrength.WEAK),
        _incident_view("INC-WEAK-1", strength=AnchorStrength.WEAK),
    ]

    decision = _evaluate_success(_strong_oom_event(), views)

    assert decision.decision_type is DecisionType.ENTER_PENDING
    assert decision.reason_code is DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES
    assert decision.anchor_strength is None
    assert decision.diagnostics == (("candidate_count", 2),)


@pytest.mark.parametrize(
    "phase,target_strength",
    [
        (EvaluationPhase.INITIAL, AnchorStrength.STRONG),
        (EvaluationPhase.INITIAL, AnchorStrength.WEAK),
        (EvaluationPhase.PENDING_RECHECK, AnchorStrength.STRONG),
        (EvaluationPhase.PENDING_RECHECK, AnchorStrength.WEAK),
        (EvaluationPhase.PENDING_EXPIRED, AnchorStrength.STRONG),
        (EvaluationPhase.PENDING_EXPIRED, AnchorStrength.WEAK),
    ],
)
def test_known_weak_unique_candidate_attaches_with_effective_anchor(
    phase,
    target_strength,
):
    target = _incident_view("INC-TARGET", strength=target_strength)

    decision = _evaluate_success(_runtime_event(), [target], phase)

    assert decision.decision_type is DecisionType.ATTACH_EXISTING
    assert decision.reason_code is DecisionReasonCode.UNIQUE_COMPATIBLE_CANDIDATE
    assert decision.target_incident_id == "INC-TARGET"
    assert decision.anchor_strength is target_strength
    assert decision.anchor_transition is AnchorTransition.NONE
    assert decision.normalized_fingerprint is None


@pytest.mark.parametrize(
    "phase,candidate_count,expected_type,expected_reason,expected_strength",
    [
        (
            EvaluationPhase.INITIAL,
            0,
            DecisionType.ENTER_PENDING,
            DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
            None,
        ),
        (
            EvaluationPhase.PENDING_RECHECK,
            0,
            DecisionType.ENTER_PENDING,
            DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
            None,
        ),
        (
            EvaluationPhase.PENDING_EXPIRED,
            0,
            DecisionType.CREATE_NEW,
            DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
            AnchorStrength.WEAK,
        ),
        (
            EvaluationPhase.INITIAL,
            2,
            DecisionType.ENTER_PENDING,
            DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES,
            None,
        ),
        (
            EvaluationPhase.PENDING_RECHECK,
            2,
            DecisionType.ENTER_PENDING,
            DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES,
            None,
        ),
        (
            EvaluationPhase.PENDING_EXPIRED,
            2,
            DecisionType.CREATE_NEW,
            DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
            AnchorStrength.WEAK,
        ),
    ],
)
def test_known_weak_unresolved_phase_matrix(
    phase,
    candidate_count,
    expected_type,
    expected_reason,
    expected_strength,
):
    views = [
        _incident_view(
            f"INC-{index}",
            strength=(
                AnchorStrength.STRONG if index % 2 == 0 else AnchorStrength.WEAK
            ),
        )
        for index in range(candidate_count)
    ]

    decision = _evaluate_success(_runtime_event(), views, phase)

    assert decision.decision_type is expected_type
    assert decision.reason_code is expected_reason
    assert decision.target_incident_id is None
    assert decision.anchor_strength is expected_strength
    assert decision.anchor_transition is AnchorTransition.NONE
    assert decision.normalized_fingerprint is None
    assert decision.diagnostics == (("candidate_count", candidate_count),)


def test_known_weak_strong_plus_weak_is_ambiguity_without_preference():
    views = [
        _incident_view("INC-WEAK", strength=AnchorStrength.WEAK),
        _incident_view("INC-STRONG", strength=AnchorStrength.STRONG),
    ]

    decision = _evaluate_success(_runtime_event(), views)

    assert decision.decision_type is DecisionType.ENTER_PENDING
    assert decision.reason_code is DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES
    assert decision.target_incident_id is None
    assert decision.diagnostics == (("candidate_count", 2),)


def test_pending_decision_uses_exact_historical_policy_reference():
    historical = _strong_policy(policy_version="1.0", is_current=False)
    current = _strong_policy(policy_version="2.0", is_current=True)
    engine = AlertCorrelationPolicyEngine(
        registry=PolicyRegistry([current, historical])
    )
    context = CorrelationEvaluationContext(
        EvaluationPhase.PENDING_RECHECK,
        "POLICY-TEST",
        "1.0",
    )

    result = engine.evaluate(
        _runtime_event("test_event", source_ip="192.0.2.1"),
        [],
        context,
    )

    assert isinstance(result, CorrelationEvaluationSuccess)
    assert result.decision.policy_id == "POLICY-TEST"
    assert result.decision.policy_version == "1.0"
    assert result.decision.decision_type is DecisionType.ENTER_PENDING


@pytest.mark.parametrize(
    "event,views,context,error_code",
    [
        (
            {},
            [],
            CorrelationEvaluationContext(EvaluationPhase.INITIAL),
            CorrelationErrorCode.INVALID_EVENT_ENVELOPE,
        ),
        (
            _runtime_event("not_registered"),
            [],
            CorrelationEvaluationContext(EvaluationPhase.INITIAL),
            CorrelationErrorCode.POLICY_NOT_REGISTERED,
        ),
        (
            _runtime_event(),
            [],
            CorrelationEvaluationContext(
                EvaluationPhase.PENDING_RECHECK,
                "POLICY-HIGH-MEMORY-DETECTED",
                "9.9",
            ),
            CorrelationErrorCode.POLICY_VERSION_UNAVAILABLE,
        ),
        (
            _runtime_event("oom_crash_detected"),
            [],
            CorrelationEvaluationContext(EvaluationPhase.INITIAL),
            CorrelationErrorCode.MISSING_REQUIRED_IDENTITY,
        ),
        (
            _runtime_event("oom_crash_detected", service_name=42),
            [],
            CorrelationEvaluationContext(EvaluationPhase.INITIAL),
            CorrelationErrorCode.INVALID_IDENTITY_VALUE,
        ),
        (
            _runtime_event(),
            [_incident_view(status="RESOLVED")],
            CorrelationEvaluationContext(EvaluationPhase.INITIAL),
            CorrelationErrorCode.INVALID_INCIDENT_VIEW,
        ),
        (
            _runtime_event(),
            [],
            CorrelationEvaluationContext(EvaluationPhase.PENDING_RECHECK),
            CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT,
        ),
    ],
)
def test_evaluate_returns_expected_domain_failures(
    event,
    views,
    context,
    error_code,
):
    result = AlertCorrelationPolicyEngine().evaluate(event, views, context)

    assert isinstance(result, CorrelationEvaluationFailure)
    assert result.error.error_code is error_code
    assert not hasattr(result, "decision")


def test_unexpected_identity_extractor_exception_propagates():
    class BuggyExtractor(IdentityExtractor):
        def extract(self, event):
            raise RuntimeError("unexpected extractor bug")

    policy = CorrelationPolicy(
        "POLICY-BUGGY",
        "1.0",
        "buggy_event",
        EvidenceClass.STRONG,
        CorrelationFamily.ATTACK_SOURCE,
        BuggyExtractor(),
    )
    engine = AlertCorrelationPolicyEngine(registry=PolicyRegistry([policy]))

    with pytest.raises(RuntimeError, match="unexpected extractor bug"):
        engine.evaluate(
            _runtime_event("buggy_event"),
            [],
            CorrelationEvaluationContext(EvaluationPhase.INITIAL),
        )


def test_evaluate_is_order_invariant_and_does_not_mutate_inputs():
    event = _runtime_event(
        triggered_features={"nested": ["preserve", "values"]}
    )
    views = [
        _incident_view("INC-WEAK", strength=AnchorStrength.WEAK),
        _incident_view("INC-STRONG", strength=AnchorStrength.STRONG),
    ]
    event_before = copy.deepcopy(event)
    views_before = copy.deepcopy(views)
    context = CorrelationEvaluationContext(EvaluationPhase.INITIAL)
    engine = AlertCorrelationPolicyEngine()

    forward = engine.evaluate(event, views, context)
    reverse = engine.evaluate(event, list(reversed(views)), context)

    assert forward == reverse
    assert event == event_before
    assert views == views_before
    assert isinstance(forward, CorrelationEvaluationSuccess)
    diagnostics = dict(forward.decision.diagnostics)
    assert diagnostics == {"candidate_count": 2}
    assert all("incident" not in key for key in diagnostics)


def test_repeated_equivalent_evaluation_is_deterministic():
    event = _strong_oom_event()
    views = [
        _incident_view("INC-EXACT-2"),
        _incident_view("INC-EXACT-1"),
        _incident_view("INC-WEAK", strength=AnchorStrength.WEAK),
    ]
    context = CorrelationEvaluationContext(EvaluationPhase.INITIAL)
    engine = AlertCorrelationPolicyEngine()

    first = engine.evaluate(event, views, context)
    second = engine.evaluate(event, views, context)

    assert first == second


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "decision_type": DecisionType.ATTACH_EXISTING,
            "reason_code": DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH,
            "normalized_fingerprint": NormalizedFingerprint(
                "event", (("id", "value"),)
            ),
            "anchor_strength": AnchorStrength.STRONG,
        },
        {
            "decision_type": DecisionType.CREATE_NEW,
            "reason_code": DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
            "normalized_fingerprint": NormalizedFingerprint(
                "event", (("id", "value"),)
            ),
        },
        {
            "decision_type": DecisionType.ENTER_PENDING,
            "reason_code": DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
            "anchor_strength": AnchorStrength.WEAK,
        },
        {
            "decision_type": DecisionType.ROUTE_SHADOW,
            "reason_code": DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY,
            "correlation_family": CorrelationFamily.UNKNOWN,
            "anchor_strength": AnchorStrength.WEAK,
        },
        {
            "decision_type": DecisionType.CREATE_NEW,
            "reason_code": DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
            "anchor_strength": AnchorStrength.WEAK,
            "anchor_transition": AnchorTransition.WEAK_TO_STRONG,
        },
        {
            "decision_type": DecisionType.ATTACH_EXISTING,
            "reason_code": DecisionReasonCode.WEAK_TO_STRONG_PROMOTION,
            "target_incident_id": "INC-1",
            "anchor_strength": AnchorStrength.STRONG,
            "anchor_transition": AnchorTransition.WEAK_TO_STRONG,
        },
        {
            "decision_type": DecisionType.ENTER_PENDING,
            "reason_code": DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES,
            "target_incident_id": "INC-1",
        },
    ],
)
def test_decision_contract_rejects_invalid_invariant_combinations(kwargs):
    values = {
        "decision_type": DecisionType.ENTER_PENDING,
        "policy_id": "POLICY-TEST",
        "policy_version": "1.0",
        "correlation_family": CorrelationFamily.ATTACK_SOURCE,
        "reason_code": DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        CorrelationDecision(**values)


def test_result_variants_require_their_exclusive_payload_type():
    with pytest.raises(TypeError):
        CorrelationEvaluationSuccess(CorrelationEvaluationError(
            CorrelationErrorCode.INVALID_EVENT_ENVELOPE,
            "",
            "",
        ))
    with pytest.raises(TypeError):
        CorrelationEvaluationFailure(CorrelationDecision(
            DecisionType.ENTER_PENDING,
            "POLICY-TEST",
            "1.0",
            CorrelationFamily.ATTACK_SOURCE,
            DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
        ))


@pytest.mark.parametrize(
    "summary,expected_fingerprint",
    [
        (
            WindowSummary(
                top_error_types=["OutOfMemoryError"],
                oom_origin_service="payment-api",
                unique_services=["unrelated-first-service"],
            ),
            NormalizedFingerprint(
                "oom_crash_detected", (("service_name", "payment-api"),)
            ),
        ),
        (
            WindowSummary(
                target_429_counts={"sms-gateway": 20},
                unique_services=["gateway-api"],
            ),
            NormalizedFingerprint(
                "rate_limit_storm", (("target_service", "sms-gateway"),)
            ),
        ),
    ],
)
def test_evaluate_consumes_real_production_event_builder_contract(
    monkeypatch,
    summary,
    expected_fingerprint,
):
    builder = EventBuilder()
    monkeypatch.setattr(builder, "_make_event_id", lambda: "EVT-PRODUCTION")
    monkeypatch.setattr(
        builder,
        "_now_iso",
        lambda: "2026-08-30T12:00:00Z",
    )
    event = builder.build(PredictionResult(True, -0.4, 0.91, -1), summary)
    assert event is not None
    assert isinstance(event, dict)
    if expected_fingerprint.event_type == "oom_crash_detected":
        assert event["service_name"] == "payment-api"
        assert event["service_name"] != "unrelated-first-service"
    else:
        assert "target_service" not in event
        assert event["triggered_features"]["target_service"] == "sms-gateway"

    decision = _evaluate_success(event)

    assert decision.decision_type is DecisionType.CREATE_NEW
    assert decision.normalized_fingerprint == expected_fingerprint
