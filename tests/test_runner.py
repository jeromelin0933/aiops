import json
from datetime import datetime, timedelta, timezone

import pytest

from src.event_detection.model.predictor import PredictionResult
from src.event_detection.runner import LogEventDetectionRunner
from src.event_detection.event.builder import EventBuilder
from src.event_detection.model.schema import WindowFeatureVector
from src.event_detection.store.event_store import EventStore


class StubPredictor:
    def __init__(self, anomaly):
        self.anomaly = anomaly
        self.vectors = []

    def predict_one(self, vector):
        self.vectors.append(vector)
        return PredictionResult(self.anomaly, -0.4 if self.anomaly else 0.1,
                                0.9 if self.anomaly else 0.0,
                                -1 if self.anomaly else 1)


def config():
    return {
        "window": {"window_seconds": 60, "min_log_count": 2},
        "event": {"cooldown_seconds": 60},
        "output": {"model_path": "unused.pkl"},
        "anomaly": {"score_threshold": -0.05,
                    "confidence_high_threshold": -0.3,
                    "confidence_medium_threshold": -0.1},
    }


def line(timestamp, status=200, source_ip=None):
    return json.dumps({"timestamp": timestamp, "level": "ERROR" if status >= 400 else "INFO",
                       "service_name": "auth", "status_code": status,
                       "duration_ms": 10, "source_ip": source_ip})


def test_single_step_writes_anomalous_event_and_respects_minimum(tmp_path):
    predictor = StubPredictor(True)
    store = EventStore(tmp_path / "events.jsonl")
    runner = LogEventDetectionRunner(config=config(), predictor=predictor, store=store)
    assert runner.process_line(line("2026-01-01T00:00:00Z")) is None
    event = runner.process_line(line("2026-01-01T00:00:01Z"))
    assert event["event_type"] == "general_log_anomaly"
    assert len(store.read_all()) == 1
    assert predictor.vectors[0].total_log_count == 2


def test_normal_prediction_does_not_write(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    runner = LogEventDetectionRunner(config=config(), predictor=StubPredictor(False), store=store)
    runner.process_line(line("2026-01-01T00:00:00Z"))
    assert runner.process_line(line("2026-01-01T00:00:01Z")) is None
    assert store.read_all() == []


def test_cooldown_uses_event_time_and_suppresses_same_type(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    runner = LogEventDetectionRunner(config=config(), predictor=StubPredictor(True), store=store)
    runner.process_line(line("2000-01-01T00:00:00Z"))
    assert runner.process_line(line("2000-01-01T00:00:01Z")) is not None
    assert runner.process_line(line("2000-01-01T00:00:20Z")) is None
    assert len(store.read_all()) == 1


def test_event_time_window_prunes_old_entries(tmp_path):
    predictor = StubPredictor(True)
    runner = LogEventDetectionRunner(config=config(), predictor=predictor,
                                     store=EventStore(tmp_path / "events.jsonl"))
    runner.process_line(line("2000-01-01T00:00:00Z"))
    assert runner.process_line(line("2000-01-01T00:02:00Z")) is None
    assert predictor.vectors == []


def test_mixed_service_window_uses_actual_oom_origin_without_changing_vector():
    logs = [
        {
            "_parsed_timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "timestamp": "2026-01-01T00:00:00Z",
            "level": "ERROR",
            "service_name": "payment-service",
            "status_code": 500,
            "duration_ms": 10,
            "error_type": "PaymentError",
        },
        {
            "_parsed_timestamp": datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            "timestamp": "2026-01-01T00:00:01Z",
            "level": "ERROR",
            "service_name": "order-service",
            "status_code": 500,
            "duration_ms": 20,
            "memory_usage_pct": 99,
            "error_type": "OutOfMemoryError",
        },
    ]

    summary = LogEventDetectionRunner._compute_summary(logs)
    # Force the legacy generic fallback candidate to be the wrong service.
    summary.unique_services = ["payment-service", "order-service"]
    assert summary.oom_origin_service == "order-service"

    event = EventBuilder().build(
        PredictionResult(True, -0.4, 0.91, -1), summary
    )
    assert event["event_type"] == "oom_crash_detected"
    assert event["service_name"] == "order-service"

    expected_names = [
        "total_log_count", "error_count", "warn_count", "error_rate",
        "warn_rate", "status_4xx_count", "status_5xx_count",
        "status_401_count", "status_429_count", "unique_service_count",
        "unique_trace_id_count", "unique_source_ip_count",
        "unique_downstream_count", "unique_external_service_count",
        "unique_target_service_count", "max_same_source_ip_count",
        "max_same_downstream_count", "max_same_target_service_count",
        "max_duration_ms", "mean_duration_ms", "max_memory_pct",
        "mean_memory_pct", "oom_count",
    ]
    assert WindowFeatureVector.feature_names() == expected_names
    assert len(WindowFeatureVector().to_list()) == len(expected_names) == 23


def test_oom_event_without_origin_metadata_fails_closed():
    summary = LogEventDetectionRunner._compute_summary([
        {
            "_parsed_timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "level": "ERROR",
            "service_name": "",
            "status_code": 500,
            "error_type": "OutOfMemoryError",
        }
    ])
    assert summary.oom_origin_service is None
    with pytest.raises(ValueError, match="requires OOM-origin service metadata"):
        EventBuilder().build(PredictionResult(True, -0.4, 0.91, -1), summary)
