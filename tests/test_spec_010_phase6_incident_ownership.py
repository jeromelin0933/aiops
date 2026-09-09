"""Phase 6 evidence that Shadow consumes SPEC-008 ownership semantically."""

from datetime import datetime, timezone

import pytest

from src.alert_correlation import (
    AlertCorrelationPolicyEngine,
    CorrelationEvaluationContext,
    CorrelationEvaluationSuccess,
    EvaluationPhase,
)
from src.alert_correlation.state import CorrelationMutationIntent as Spec008MutationIntent
from src.incident_management import (
    IncidentDomainError,
    IncidentErrorCode,
    IncidentManager,
    IncidentMutationRequest,
    SqliteIncidentStore,
)
from alert_correlation.contracts import (
    AnchorTransition,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
)
from alert_correlation.policy import DEFAULT_POLICY_REGISTRY
from alert_correlation.state.contracts import CorrelationMutationIntent, TerminalOutcome
from shadow_management.contracts import ShadowDomainError, ShadowDomainErrorCode
from shadow_management.manager import ShadowManager
from shadow_management.sqlite_store import SqliteShadowStore


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _runtime_event(event_id: str, event_type: str, *, source_ip: str | None = None) -> dict[str, object]:
    return {
        "event_id": event_id,
        "detected_at": "2026-09-09T12:00:00Z",
        "event_source": "log_event_detection",
        "event_type": event_type,
        "detection_method": "rule_based",
        "severity": "HIGH",
        "confidence": 0.99,
        "service_name": "auth-api",
        "trace_id": None,
        "source_ip": source_ip,
        "downstream_service": None,
        "external_service": None,
        "status": "OPEN",
        "triggered_features": {},
        "raw_log_sample": [],
    }


def _shadow_intent(operation_id: str, event_id: str) -> CorrelationMutationIntent:
    return CorrelationMutationIntent(
        operation_id, event_id, TerminalOutcome.SHADOWED,
        DecisionType.ROUTE_SHADOW, "POLICY-GENERAL-LOG-ANOMALY", "1.0",
        CorrelationFamily.UNKNOWN,
        DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY,
        None, None, None, AnchorTransition.NONE, NOW,
    )


def _shadow_event(event_id: str) -> dict[str, object]:
    return {"event_id": event_id, "event_type": "general_log_anomaly"}


def _seed_incident_owner(store: SqliteIncidentStore, event_id: str) -> None:
    event = _runtime_event(event_id, "brute_force_detected", source_ip="203.0.113.10")
    decision = AlertCorrelationPolicyEngine().evaluate(
        event, (), CorrelationEvaluationContext(EvaluationPhase.INITIAL)
    )
    assert isinstance(decision, CorrelationEvaluationSuccess)
    intent = Spec008MutationIntent.from_decision(
        operation_id="OP-INCIDENT",
        event_id=event_id,
        decision=decision.decision,
        created_at=NOW,
    )
    IncidentManager(store, incident_id_factory=lambda: "INC-1").apply_correlation_mutation(
        IncidentMutationRequest(intent, event, NOW)
    )


def _manager(shadow_path, incident_store: SqliteIncidentStore) -> tuple[SqliteShadowStore, ShadowManager]:
    shadow_store = SqliteShadowStore(shadow_path)
    return shadow_store, ShadowManager(
        shadow_store, DEFAULT_POLICY_REGISTRY, incident_store,
        shadow_id_factory=lambda: "SHADOW-1",
    )


def test_real_spec_008_clean_absence_allows_durable_shadow_replay(tmp_path) -> None:
    with SqliteIncidentStore(str(tmp_path / "incident.sqlite")) as incidents:
        shadows, manager = _manager(tmp_path / "shadow.sqlite", incidents)
        original = manager.route_shadow(_shadow_intent("OP-1", "EVT-1"), _shadow_event("EVT-1"), NOW)
        replay = manager.route_shadow(_shadow_intent("OP-1", "EVT-1"), _shadow_event("EVT-1"), NOW)
        assert incidents.event_has_incident_owner("EVT-1") is False
        assert replay == original
        assert shadows.get_shadow_by_event_id("EVT-1").shadow_id == "SHADOW-1"
        assert len(shadows.enumerate_shadows()) == 1
        shadows.close()


def test_real_spec_008_incident_owner_rejects_shadow_before_local_write(tmp_path) -> None:
    with SqliteIncidentStore(str(tmp_path / "incident.sqlite")) as incidents:
        _seed_incident_owner(incidents, "EVT-1")
        shadows, manager = _manager(tmp_path / "shadow.sqlite", incidents)
        with pytest.raises(ShadowDomainError) as raised:
            manager.route_shadow(_shadow_intent("OP-SHADOW", "EVT-1"), _shadow_event("EVT-1"), NOW)
        assert raised.value.code is ShadowDomainErrorCode.INCIDENT_EVENT_OWNERSHIP_CONFLICT
        assert shadows.get_shadow_by_event_id("EVT-1") is None
        assert shadows.get_operation_result("OP-SHADOW") is None
        shadows.close()


def test_spec_008_ownership_integrity_failure_fails_closed_before_shadow_write(tmp_path, monkeypatch) -> None:
    with SqliteIncidentStore(str(tmp_path / "incident.sqlite")) as incidents:
        shadows, manager = _manager(tmp_path / "shadow.sqlite", incidents)

        def corrupt_read(_event_id: str) -> bool:
            raise IncidentDomainError(
                IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE, "corrupt ownership"
            )

        monkeypatch.setattr(incidents, "event_has_incident_owner", corrupt_read)
        with pytest.raises(ShadowDomainError) as raised:
            manager.route_shadow(_shadow_intent("OP-1", "EVT-1"), _shadow_event("EVT-1"), NOW)
        assert raised.value.code is ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE
        assert shadows.get_shadow_by_event_id("EVT-1") is None
        assert shadows.get_operation_result("OP-1") is None
        shadows.close()


def test_restart_preserves_real_spec_008_ownership_rejection(tmp_path) -> None:
    incident_path = tmp_path / "incident.sqlite"
    shadow_path = tmp_path / "shadow.sqlite"
    with SqliteIncidentStore(str(incident_path)) as incidents:
        _seed_incident_owner(incidents, "EVT-1")

    with SqliteIncidentStore(str(incident_path)) as reopened_incidents:
        shadows, manager = _manager(shadow_path, reopened_incidents)
        with pytest.raises(ShadowDomainError) as raised:
            manager.route_shadow(_shadow_intent("OP-SHADOW", "EVT-1"), _shadow_event("EVT-1"), NOW)
        assert raised.value.code is ShadowDomainErrorCode.INCIDENT_EVENT_OWNERSHIP_CONFLICT
        assert shadows.get_shadow_by_event_id("EVT-1") is None
        shadows.close()
