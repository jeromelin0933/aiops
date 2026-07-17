import json
from datetime import datetime, timedelta, timezone

from src.event_detection.model.predictor import PredictionResult
from src.event_detection.runner import LogEventDetectionRunner
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
