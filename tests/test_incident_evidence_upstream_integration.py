from datetime import datetime, timezone
import inspect

from alert_correlation import (
    AlertCorrelationPolicyEngine,
    CorrelationEvaluationContext,
    CorrelationEvaluationSuccess,
    EvaluationPhase,
)
from alert_correlation.state import CorrelationMutationIntent
from event_detection.store.event_store import EventStore
from incident_evidence import CaptureCommand, admit_capture_plan, load_evidence_policy
import incident_evidence.capture_plan as capture_plan_module
import incident_evidence.trusted_core as trusted_core_module
from incident_management import (
    IncidentManager,
    IncidentMutationRequest,
    SqliteIncidentStore,
)


UTC = timezone.utc


def _event():
    return {
        "event_id": "EVT-REAL",
        "detected_at": "2026-09-22T01:00:00Z",
        "event_source": "log_event_detection",
        "event_type": "brute_force_detected",
        "detection_method": "isolation_forest",
        "severity": "CRITICAL",
        "confidence": 0.99,
        "service_name": "auth-api",
        "trace_id": None,
        "source_ip": "203.0.113.10",
        "downstream_service": None,
        "external_service": None,
        "status": "OPEN",
        "triggered_features": {},
        "raw_log_sample": [],
    }


def test_real_event_store_and_public_incident_read_admit_plan(tmp_path):
    event = _event()
    result = AlertCorrelationPolicyEngine().evaluate(
        event,
        (),
        CorrelationEvaluationContext(EvaluationPhase.INITIAL),
    )
    assert isinstance(result, CorrelationEvaluationSuccess)
    intent = CorrelationMutationIntent.from_decision(
        operation_id="OP-REAL",
        event_id=event["event_id"],
        decision=result.decision,
        created_at=datetime(2026, 9, 22, 1, 0, tzinfo=UTC),
    )
    event_store = EventStore(str(tmp_path / "events.jsonl"))
    event_store.write(event)
    policy = load_evidence_policy("configs/incident_evidence.yaml")
    command = CaptureCommand(
        "CAP-REAL",
        "INC-REAL",
        datetime(2026, 9, 22, 1, 2, tzinfo=UTC),
        policy.capture_contract_version,
        policy.canonicalization_version,
        policy.source_policy_version,
        policy.bounds_policy_version,
        policy.config_identity,
    )

    with SqliteIncidentStore(str(tmp_path / "incidents.db")) as incident_store:
        IncidentManager(
            incident_store, incident_id_factory=lambda: "INC-REAL"
        ).apply_correlation_mutation(
            IncidentMutationRequest(
                intent,
                event,
                datetime(2026, 9, 22, 1, 1, tzinfo=UTC),
            )
        )
        plan = admit_capture_plan(command, incident_store, event_store, policy)

    assert plan.incident.event_ids == ("EVT-REAL",)
    assert plan.events[0].event_id == "EVT-REAL"


def test_phase_3_has_no_ground_truth_scenario_or_generator_dependency():
    source = inspect.getsource(trusted_core_module) + inspect.getsource(capture_plan_module)
    lowered = source.lower()
    assert "ground_truth" not in lowered
    assert "scenario" not in lowered
    assert "generator" not in lowered
