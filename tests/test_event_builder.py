import json
from pathlib import Path

import pytest

from src.event_detection.event.builder import EVENT_FIELDS, EventBuilder
from src.event_detection.model.predictor import PredictionResult
from src.event_detection.model.schema import WindowSummary


FIXTURE = Path(__file__).parent / "fixtures" / "phase3" / "scenarios.json"
ANOMALY = PredictionResult(True, -0.4, 0.91, -1)


@pytest.mark.parametrize("scenario,event_type,severity", [
    ("S1", "brute_force_detected", "CRITICAL"),
    ("S2", "cross_service_failure", "HIGH"),
    ("S3", "oom_crash_detected", "CRITICAL"),
    ("S4", "external_dependency_failure", "HIGH"),
    ("S5", "downstream_cascade_failure", "CRITICAL"),
    ("S6", "rate_limit_storm", "HIGH"),
])
def test_classifies_scenarios(scenario, event_type, severity):
    values = json.loads(FIXTURE.read_text(encoding="utf-8"))[scenario]
    summary = WindowSummary(unique_services=["service"], total_log_count=20,
                            error_count=10, raw_log_sample=[{}] * 5, **values)
    event = EventBuilder().build(ANOMALY, summary)
    assert event["event_type"] == event_type
    assert event["severity"] == severity
    assert set(event) == EVENT_FIELDS
    assert event["detection_method"] == "isolation_forest"
    assert event["status"] == "OPEN"
    assert event["confidence"] == 0.91
    assert len(event["raw_log_sample"]) == 3


def test_normal_prediction_returns_none():
    normal = PredictionResult(False, 0.1, 0.0, 1)
    assert EventBuilder().build(normal, WindowSummary()) is None


def test_unknown_anomaly_uses_fallback():
    event = EventBuilder().build(ANOMALY, WindowSummary())
    assert (event["event_type"], event["severity"]) == ("general_log_anomaly", "MEDIUM")


def test_fixed_priority_prefers_s3_then_s5():
    summary = WindowSummary(
        top_error_types=["OutOfMemoryError"],
        oom_origin_service="order-service",
        downstream_error_services={"db": ["a", "b", "c", "d", "e"]},
        source_ip_401_counts={"ip": 10}, target_429_counts={"sms": 20},
    )
    assert EventBuilder().build(ANOMALY, summary)["event_type"] == "oom_crash_detected"
    summary.top_error_types = []
    assert EventBuilder().build(ANOMALY, summary)["event_type"] == "downstream_cascade_failure"
