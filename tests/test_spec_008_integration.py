"""Cross-SPEC integration evidence for SPEC-008 Incident authority."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import inspect
import re

import pytest

from src.alert_correlation import (
    AlertCorrelationPolicyEngine,
    CorrelationEvaluationContext,
    CorrelationEvaluationSuccess,
    DecisionType,
    EvaluationPhase,
    IncidentCorrelationView,
)
from src.alert_correlation.state import (
    CorrelationMutationIntent,
    RetryDisposition,
    StateDomainErrorCode,
    StateDomainValidationError,
    TerminalOutcome,
)
from src.incident_management import (
    IncidentDomainError,
    IncidentErrorCode,
    IncidentManager,
    IncidentMutationCompletion,
    IncidentMutationRequest,
    IncidentStatus,
    SqliteIncidentStore,
)
import src.incident_management.contracts as incident_contracts
import src.incident_management.manager as incident_manager_module
import src.incident_management.sqlite_store as incident_store_module


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _event(
    event_id: str,
    event_type: str,
    detected_at: str,
    *,
    severity: str = "HIGH",
    source_ip: str | None = None,
    service_name: str = "auth-api",
) -> dict[str, object]:
    """A real PRD-002 15-field Runtime Event mapping."""
    return {
        "event_id": event_id,
        "detected_at": detected_at,
        "event_source": "log_event_detection",
        "event_type": event_type,
        "detection_method": "rule_based",
        "severity": severity,
        "confidence": 0.99,
        "service_name": service_name,
        "trace_id": None,
        "source_ip": source_ip,
        "downstream_service": None,
        "external_service": None,
        "status": "OPEN",
        "triggered_features": {},
        "raw_log_sample": [],
    }


def _decision(event: dict[str, object], views=()):
    result = AlertCorrelationPolicyEngine().evaluate(
        event,
        views,
        CorrelationEvaluationContext(EvaluationPhase.INITIAL),
    )
    assert isinstance(result, CorrelationEvaluationSuccess)
    return result.decision


def _request(
    decision,
    event: dict[str, object],
    operation_id: str,
    *,
    now: datetime,
) -> IncidentMutationRequest:
    intent = CorrelationMutationIntent.from_decision(
        operation_id=operation_id,
        event_id=event["event_id"],
        decision=decision,
        created_at=now,
    )
    return IncidentMutationRequest(intent, event, now)


def test_real_create_decision_and_intent_produce_spec_008_result(tmp_path):
    event = _event(
        "EVT-CREATE",
        "brute_force_detected",
        "2026-09-09T12:00:00Z",
        source_ip="203.0.113.10",
    )
    unchanged_event = deepcopy(event)
    decision = _decision(event)
    assert decision.decision_type is DecisionType.CREATE_NEW

    request = _request(decision, event, "OP-CREATE", now=NOW)
    assert isinstance(request.intent, CorrelationMutationIntent)
    assert request.intent.intended_terminal_outcome is TerminalOutcome.CREATED_INCIDENT

    with SqliteIncidentStore(str(tmp_path / "incident.db")) as store:
        result = IncidentManager(
            store, incident_id_factory=lambda: "INC-CREATED"
        ).apply_correlation_mutation(request)
        incident = store.get_incident(result.incident_id)

    assert result.operation_id == "OP-CREATE"
    assert result.event_id == "EVT-CREATE"
    assert result.mutation_kind is DecisionType.CREATE_NEW
    assert result.completion_result is IncidentMutationCompletion.SUCCEEDED
    assert result.incident_id == "INC-CREATED"
    assert incident.status is IncidentStatus.OPEN
    assert incident.event_ids == ("EVT-CREATE",)
    assert event == unchanged_event


def test_real_store_view_drives_spec_006_exact_target_attach(tmp_path):
    first = _event(
        "EVT-1",
        "brute_force_detected",
        "2026-09-09T12:00:00Z",
        severity="MEDIUM",
        source_ip="203.0.113.20",
    )
    second = _event(
        "EVT-2",
        "brute_force_detected",
        "2026-09-09T12:01:00Z",
        severity="CRITICAL",
        source_ip="203.0.113.20",
    )

    with SqliteIncidentStore(str(tmp_path / "incident.db")) as store:
        manager = IncidentManager(store, incident_id_factory=lambda: "INC-TARGET")
        manager.apply_correlation_mutation(
            _request(_decision(first), first, "OP-CREATE", now=NOW)
        )

        views = store.list_correlation_views()
        assert len(views) == 1
        assert isinstance(views[0], IncidentCorrelationView)
        attach_decision = _decision(second, views)
        assert attach_decision.decision_type is DecisionType.ATTACH_EXISTING
        assert attach_decision.target_incident_id == "INC-TARGET"

        result = manager.apply_correlation_mutation(
            _request(
                attach_decision,
                second,
                "OP-ATTACH",
                now=NOW + timedelta(minutes=1),
            )
        )
        incident = store.get_incident("INC-TARGET")

    assert result.mutation_kind is DecisionType.ATTACH_EXISTING
    assert result.incident_id == "INC-TARGET"
    assert incident.event_ids == ("EVT-1", "EVT-2")


def test_real_route_shadow_intent_is_rejected_by_spec_008_boundary():
    event = _event(
        "EVT-SHADOW",
        "general_log_anomaly",
        "2026-09-09T12:00:00Z",
        severity="LOW",
    )
    decision = _decision(event)
    assert decision.decision_type is DecisionType.ROUTE_SHADOW
    intent = CorrelationMutationIntent.from_decision(
        operation_id="OP-SHADOW",
        event_id=event["event_id"],
        decision=decision,
        created_at=NOW,
    )
    assert intent.intended_terminal_outcome is TerminalOutcome.SHADOWED

    with pytest.raises(IncidentDomainError) as raised:
        IncidentMutationRequest(intent, event, NOW)

    assert raised.value.code is IncidentErrorCode.INVALID_INCIDENT_MUTATION
    assert raised.value.retry_disposition is RetryDisposition.NON_RETRYABLE


def test_enter_pending_decision_cannot_construct_real_mutation_intent():
    event = _event(
        "EVT-PENDING",
        "high_memory_detected",
        "2026-09-09T12:00:00Z",
        severity="MEDIUM",
        service_name="payments",
    )
    decision = _decision(event)
    assert decision.decision_type is DecisionType.ENTER_PENDING

    with pytest.raises(StateDomainValidationError) as raised:
        CorrelationMutationIntent.from_decision(
            operation_id="OP-PENDING",
            event_id=event["event_id"],
            decision=decision,
            created_at=NOW,
        )

    assert raised.value.code is StateDomainErrorCode.MUTATION_INTENT_CONFLICT


def test_crash_c_reopen_same_operation_recovers_original_result(tmp_path):
    path = tmp_path / "incident.db"
    event = _event(
        "EVT-CRASH-C",
        "brute_force_detected",
        "2026-09-09T12:00:00Z",
        source_ip="203.0.113.30",
    )
    request = _request(_decision(event), event, "OP-CRASH-C", now=NOW)

    with SqliteIncidentStore(str(path)) as store:
        original = IncidentManager(
            store, incident_id_factory=lambda: "INC-CRASH-C"
        ).apply_correlation_mutation(request)
        original_incident = store.get_incident("INC-CRASH-C")

    retry = IncidentMutationRequest(
        request.intent,
        event,
        NOW + timedelta(days=1),
    )
    with SqliteIncidentStore(str(path)) as reopened:
        replay = IncidentManager(
            reopened, incident_id_factory=lambda: "INC-MUST-NOT-BE-CREATED"
        ).apply_correlation_mutation(retry)
        recovered = reopened.get_operation_result("OP-CRASH-C")
        replayed_incident = reopened.get_incident("INC-CRASH-C")

    assert replay == recovered == original
    assert replay.completed_at == NOW
    assert replayed_incident == original_incident
    assert replayed_incident.event_ids == ("EVT-CRASH-C",)
    assert len(replayed_incident.audit_trail) == 1


def test_spec_008_has_no_spec_007_store_or_processing_claim_coupling():
    modules = (
        incident_contracts,
        incident_manager_module,
        incident_store_module,
    )
    for module in modules:
        assert "ProcessingClaim" not in vars(module)
        assert "SqliteCorrelationStateStore" not in vars(module)

    store_source = inspect.getsource(incident_store_module)
    direct_state_table_access = re.compile(
        r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+correlation_state_",
        re.IGNORECASE,
    )
    assert direct_state_table_access.search(store_source) is None

