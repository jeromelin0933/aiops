from datetime import datetime, timezone

import pytest

from src.event_detection.log.window import WindowFeatureAggregator
from src.event_detection.model.schema import RawFeatures, WindowFeatureVector


def test_window_vector_has_stable_23_dimensions():
    vector = WindowFeatureVector()
    assert len(vector.to_list()) == 23
    assert len(vector.feature_names()) == 23
    assert vector.to_list() == [getattr(vector, name) for name in vector.feature_names()]


def test_aggregates_required_window_statistics():
    logs = [
        {"timestamp": "2026-07-17T00:00:00Z", "level": "INFO", "status_code": 200,
         "duration_ms": 10, "memory_usage_pct": 10, "service_name": "a"},
        {"timestamp": "2026-07-17T00:00:20Z", "level": "ERROR", "status_code": 401,
         "duration_ms": 30, "memory_usage_pct": 30, "service_name": "a",
         "trace_id": "t1", "source_ip": "ip1", "downstream_service": "db",
         "target_service": "sms"},
        {"timestamp": "2026-07-17T00:00:40Z", "level": "WARN", "status_code": 429,
         "duration_ms": 50, "memory_usage_pct": 50, "service_name": "b",
         "trace_id": "t2", "source_ip": "ip1", "downstream_service": "db",
         "external_service": "bank", "target_service": "sms",
         "error_type": "OutOfMemoryError"},
    ]
    vector = WindowFeatureAggregator(min_log_count=3).aggregate(logs)
    assert (vector.total_log_count, vector.error_count, vector.warn_count) == (3, 1, 1)
    assert vector.error_rate == pytest.approx(1 / 3)
    assert (vector.status_401_count, vector.status_429_count) == (1, 1)
    assert (vector.unique_service_count, vector.unique_trace_id_count) == (2, 2)
    assert (vector.unique_source_ip_count, vector.unique_downstream_count) == (1, 1)
    assert (vector.unique_external_service_count, vector.unique_target_service_count) == (1, 1)
    assert (vector.max_same_source_ip_count, vector.max_same_downstream_count) == (2, 2)
    assert vector.max_same_target_service_count == 2
    assert (vector.max_duration_ms, vector.mean_duration_ms) == (50, 30)
    assert (vector.max_memory_pct, vector.mean_memory_pct) == (50, 30)
    assert vector.oom_count == 1


def test_event_time_window_is_inclusive_and_does_not_use_wall_clock():
    logs = [
        {"_parsed_timestamp": datetime(2000, 1, 1, 0, 0, tzinfo=timezone.utc),
         "level": "INFO", "status_code": 200, "duration_ms": 1},
        {"timestamp": "2000-01-01T00:01:00Z", "level": "ERROR",
         "status_code": 500, "duration_ms": 2},
        {"timestamp": "1999-12-31T23:59:59Z", "level": "ERROR",
         "status_code": 500, "duration_ms": 3},
    ]
    vector = WindowFeatureAggregator(window_seconds=60).aggregate(
        logs, window_end="2000-01-01T00:01:00Z"
    )
    assert vector.total_log_count == 2
    assert vector.error_count == 1


def test_raw_features_and_minimum_count_are_supported():
    entries = [RawFeatures(raw_timestamp="2026-01-01T00:00:00Z") for _ in range(2)]
    aggregator = WindowFeatureAggregator(min_log_count=3)
    assert not aggregator.has_enough(entries)
    assert aggregator.has_enough(entries + [entries[0]])
    assert aggregator.aggregate(entries).total_log_count == 2
