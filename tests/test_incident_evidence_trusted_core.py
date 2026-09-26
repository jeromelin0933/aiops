from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from alert_correlation import AnchorStrength, CorrelationFamily, NormalizedFingerprint
from incident_evidence import (
    CaptureCommand,
    EvidenceDomainError,
    EvidenceFailureKind,
    admit_capture_plan,
    load_evidence_policy,
)
from incident_management import IncidentCorrelationContext, IncidentSeverity, IncidentStatus


UTC = timezone.utc


def event(event_id="EVT-1", detected_at="2026-09-22T01:00:00Z", **overrides):
    value = {
        "event_id": event_id,
        "detected_at": detected_at,
        "event_source": "log_event_detection",
        "event_type": "brute_force_detected",
        "detection_method": "isolation_forest",
        "severity": "CRITICAL",
        "confidence": 0.95,
        "service_name": "auth-api",
        "trace_id": None,
        "source_ip": "192.0.2.10",
        "downstream_service": None,
        "external_service": None,
        "status": "OPEN",
        "triggered_features": {},
        "raw_log_sample": [],
    }
    value.update(overrides)
    return value


def incident(event_ids=("EVT-1",), **overrides):
    fingerprint = NormalizedFingerprint.from_mapping(
        "brute_force_detected", {"source_ip": "192.0.2.10"}
    )
    values = {
        "incident_id": "INC-1",
        "event_ids": event_ids,
        "status": IncidentStatus.OPEN,
        "severity": IncidentSeverity.CRITICAL,
        "created_at": datetime(2026, 9, 22, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 22, 1, 2, tzinfo=UTC),
        "last_correlated_at": datetime(2026, 9, 22, 1, 1, tzinfo=UTC),
        "anchor_event_id": event_ids[0] if event_ids else None,
        "correlation_context": IncidentCorrelationContext(
            CorrelationFamily.ATTACK_SOURCE,
            AnchorStrength.STRONG,
            event_ids[0] if event_ids else "EVT-1",
            "brute_force_detected",
            fingerprint,
            "policy-1",
            "1.0",
        ),
    }
    values.update(overrides)
    return type("IncidentView", (), values)()


def command():
    policy = load_evidence_policy("configs/incident_evidence.yaml")
    return CaptureCommand(
        "CAP-1",
        "INC-1",
        datetime(2026, 9, 22, 1, 4, tzinfo=UTC),
        policy.capture_contract_version,
        policy.canonicalization_version,
        policy.source_policy_version,
        policy.bounds_policy_version,
        policy.config_identity,
    )


class IncidentReader:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def get_incident(self, incident_id):
        value = self.values[self.calls]
        self.calls += 1
        return value


class EventReader:
    def __init__(self, values, error=None):
        self.values = values
        self.error = error
        self.calls = 0

    def read_all_authoritative(self):
        self.calls += 1
        if self.error:
            raise self.error
        return deepcopy(self.values)


def admit(incidents, events):
    incident_reader = IncidentReader(*incidents)
    event_reader = EventReader(events)
    plan = admit_capture_plan(
        command(), incident_reader, event_reader,
        load_evidence_policy("configs/incident_evidence.yaml"),
    )
    return plan, incident_reader, event_reader


def test_stable_i1_i2_admits_and_enumerates_events_exactly_once():
    value = incident()
    plan, incident_reader, event_reader = admit((value, value), [event()])

    assert plan.incident.incident_id == "INC-1"
    assert incident_reader.calls == 2
    assert event_reader.calls == 1


def test_consumed_incident_change_fails_before_planning():
    first = incident()
    second = incident(updated_at=datetime(2026, 9, 22, 1, 3, tzinfo=UTC))
    with pytest.raises(EvidenceDomainError) as captured:
        admit((first, second), [event()])
    assert captured.value.kind is EvidenceFailureKind.INCIDENT_CHANGED_DURING_CAPTURE


def test_incident_disappearing_at_i2_is_a_stabilization_change():
    with pytest.raises(EvidenceDomainError) as captured:
        admit((incident(), None), [event()])
    assert captured.value.kind is EvidenceFailureKind.INCIDENT_CHANGED_DURING_CAPTURE


@pytest.mark.parametrize(
    ("events", "kind"),
    [
        ([], EvidenceFailureKind.REFERENCED_EVENT_NOT_FOUND),
        ([event(), event()], EvidenceFailureKind.DUPLICATE_EVENT_ID),
        ([event(detected_at="not-a-time")], EvidenceFailureKind.INVALID_REQUIRED_EVENT),
        ([event(confidence=float("nan"))], EvidenceFailureKind.INVALID_REQUIRED_EVENT),
    ],
)
def test_missing_duplicate_and_malformed_events_fail_closed(events, kind):
    value = incident()
    with pytest.raises(EvidenceDomainError) as captured:
        admit((value, value), events)
    assert captured.value.kind is kind


def test_event_store_integrity_error_is_typed_and_not_degraded():
    reader = EventReader([], OSError("unreadable"))
    with pytest.raises(EvidenceDomainError) as captured:
        admit_capture_plan(
            command(), IncidentReader(incident()), reader,
            load_evidence_policy("configs/incident_evidence.yaml"),
        )
    assert captured.value.kind is EvidenceFailureKind.AUTHORITATIVE_EVENT_ENUMERATION_FAILURE
    assert reader.calls == 1


def test_invalid_event_source_type_method_combination_fails_closed():
    value = incident()
    invalid = event(
        event_source="metrics_threshold_detection",
        event_type="brute_force_detected",
        detection_method="isolation_forest",
    )
    with pytest.raises(EvidenceDomainError) as captured:
        admit((value, value), [invalid])
    assert captured.value.kind is EvidenceFailureKind.INVALID_REQUIRED_EVENT


def test_non_upstream_rule_based_method_fails_closed():
    value = incident()
    with pytest.raises(EvidenceDomainError) as captured:
        admit((value, value), [event(detection_method="rule_based")])
    assert captured.value.kind is EvidenceFailureKind.INVALID_REQUIRED_EVENT


def test_strong_anchor_event_type_contradiction_fails_closed():
    value = incident()
    with pytest.raises(EvidenceDomainError) as captured:
        admit((value, value), [event(event_type="rate_limit_storm")])
    assert captured.value.kind is EvidenceFailureKind.CONTRADICTORY_EVENT_IDENTITY


def test_strong_fingerprint_identity_contradiction_fails_closed():
    value = incident()
    with pytest.raises(EvidenceDomainError) as captured:
        admit((value, value), [event(source_ip="198.51.100.77")])
    assert captured.value.kind is EvidenceFailureKind.CONTRADICTORY_EVENT_IDENTITY


def test_duplicate_and_empty_incident_event_ids_fail_before_enumeration():
    for ids in ((), ("EVT-1", "EVT-1")):
        reader = EventReader([event()])
        with pytest.raises(EvidenceDomainError) as captured:
            admit_capture_plan(
                command(), IncidentReader(incident(ids)), reader,
                load_evidence_policy("configs/incident_evidence.yaml"),
            )
        assert captured.value.kind is EvidenceFailureKind.MALFORMED_INCIDENT_STATE
        assert reader.calls == 0
