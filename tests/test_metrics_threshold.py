import json
from copy import deepcopy
from datetime import datetime, timezone
from urllib.error import HTTPError

import pytest
import yaml

from src.event_detection.metrics_threshold import (
    ALLOWED_THRESHOLD_EVENT_TYPES,
    EVENT_FIELDS,
    ConfigLoader,
    CooldownManager,
    MetricValue,
    MetricsFetcher,
    MetricsThresholdDetector,
    MetricsThresholdEventBuilder,
    ThresholdEvaluator,
    ThresholdResult,
)
from src.event_detection.store.event_store import EventStore


BASE_CONFIG = {
    "prometheus": {
        "base_url": "http://localhost:9090",
        "query_timeout_seconds": 5,
    },
    "polling": {"interval_seconds": 15},
    "cooldown": {"seconds": 60},
    "output": {"event_store_path": "events/event_store.jsonl"},
    "metrics": {
        "system_memory_usage_pct": {
            "enabled": True,
            "threshold_type": "upper",
            "threshold": 90.0,
            "event_type": "high_memory_detected",
            "severity": "HIGH",
        },
        "api_p95_latency_ms": {
            "enabled": True,
            "threshold_type": "upper",
            "threshold": 3000.0,
            "event_type": "high_latency_detected",
            "severity": "HIGH",
        },
        "api_requests_per_sec": {
            "enabled": False,
            "note": "request_spike_detected belongs to SPEC-003",
        },
        "db_pool_active_connections": {
            "enabled": False,
            "note": "No formal PRD-002 threshold event_type yet.",
        },
    },
}


FIXED_NOW = datetime(2026, 7, 17, 10, 0, 1, 234000, tzinfo=timezone.utc)


def write_config(tmp_path, config=None):
    path = tmp_path / "thresholds.yaml"
    path.write_text(
        yaml.safe_dump(config or BASE_CONFIG, sort_keys=False),
        encoding="utf-8",
    )
    return path


def metric(name, value):
    return MetricValue(name=name, value=value, queried_at="2026-07-17T10:00:00.000Z")


def evaluator():
    return ThresholdEvaluator(BASE_CONFIG["metrics"])


def threshold_result(event_type="high_memory_detected", severity="HIGH"):
    return ThresholdResult(
        metric_name="system_memory_usage_pct",
        current_value=92.5,
        threshold_value=90.0,
        threshold_type="upper",
        exceeded_by=2.5,
        event_type=event_type,
        severity=severity,
        queried_at="2026-07-17T10:00:00.000Z",
        query_timestamp=1720000000.0,
    )


def builder():
    return MetricsThresholdEventBuilder(
        clock=lambda: FIXED_NOW,
        uuid_provider=lambda: "a3f9",
    )


def test_config_loader_loads_valid_config(tmp_path):
    config = ConfigLoader(write_config(tmp_path)).load()

    assert config["metrics"]["system_memory_usage_pct"]["threshold"] == 90.0
    assert config["metrics"]["api_requests_per_sec"]["enabled"] is False
    assert config["metrics"]["db_pool_active_connections"]["enabled"] is False


def test_config_loader_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ConfigLoader(tmp_path / "missing.yaml").load()


def test_config_loader_missing_required_field_raises(tmp_path):
    config = dict(BASE_CONFIG)
    config.pop("prometheus")

    with pytest.raises(ValueError):
        ConfigLoader(write_config(tmp_path, config)).load()


def test_config_loader_rejects_invalid_threshold_type(tmp_path):
    config = deepcopy(BASE_CONFIG)
    config["metrics"]["system_memory_usage_pct"]["threshold_type"] = "lower"

    with pytest.raises(ValueError):
        ConfigLoader(write_config(tmp_path, config)).load()


def test_config_loader_rejects_unsupported_enabled_event_type(tmp_path):
    config = deepcopy(BASE_CONFIG)
    config["metrics"]["system_memory_usage_pct"]["event_type"] = "request_spike_detected"

    with pytest.raises(ValueError):
        ConfigLoader(write_config(tmp_path, config)).load()


def test_allowed_threshold_event_types_are_limited():
    assert ALLOWED_THRESHOLD_EVENT_TYPES == {
        "high_memory_detected",
        "high_latency_detected",
    }


def test_metrics_fetcher_parses_success_response():
    def fake_http_client(url, timeout):
        assert "query=system_memory_usage_pct" in url
        assert timeout == 5
        return {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {}, "value": [1720000000.0, "92.5"]}],
            },
        }

    fetcher = MetricsFetcher(
        "http://localhost:9090",
        5,
        http_client=fake_http_client,
        clock=lambda: FIXED_NOW,
    )

    value = fetcher.fetch_one("system_memory_usage_pct")

    assert value.value == 92.5
    assert value.query_timestamp == 1720000000.0
    assert value.error is None
    assert value.queried_at == "2026-07-17T10:00:01.234Z"


def test_metrics_fetcher_accepts_json_string_response():
    payload = json.dumps(
        {
            "status": "success",
            "data": {"result": [{"metric": {}, "value": [1720000000.0, "3000"]}]},
        }
    )
    fetcher = MetricsFetcher("http://localhost:9090", http_client=lambda _u, _t: payload)

    assert fetcher.fetch_one("api_p95_latency_ms").value == 3000.0


def test_metrics_fetcher_empty_result_is_unavailable():
    fetcher = MetricsFetcher(
        "http://localhost:9090",
        http_client=lambda _u, _t: {"status": "success", "data": {"result": []}},
    )

    value = fetcher.fetch_one("system_memory_usage_pct")

    assert value.value is None
    assert value.error is None
    assert not value.is_available


@pytest.mark.parametrize("raw_value", ["NaN", "Inf", "+Inf", "-Inf"])
def test_metrics_fetcher_non_finite_result_is_unavailable(raw_value):
    fetcher = MetricsFetcher(
        "http://localhost:9090",
        http_client=lambda _u, _t: {
            "status": "success",
            "data": {"result": [{"metric": {}, "value": [1720000000.0, raw_value]}]},
        },
    )

    value = fetcher.fetch_one("system_memory_usage_pct")

    assert value.value is None
    assert value.error is None
    assert not value.is_available


def test_metrics_fetcher_timeout_becomes_unavailable_metric():
    def failing_http_client(_url, _timeout):
        raise TimeoutError("timeout")

    fetcher = MetricsFetcher("http://localhost:9090", http_client=failing_http_client)

    value = fetcher.fetch_one("system_memory_usage_pct")

    assert value.value is None
    assert "timeout" in value.error
    assert not value.is_available


def test_metrics_fetcher_connection_error_becomes_unavailable_metric():
    def failing_http_client(_url, _timeout):
        raise ConnectionError("network unavailable")

    fetcher = MetricsFetcher("http://localhost:9090", http_client=failing_http_client)

    value = fetcher.fetch_one("system_memory_usage_pct")

    assert value.value is None
    assert "network unavailable" in value.error
    assert not value.is_available


def test_metrics_fetcher_http_error_becomes_unavailable_metric():
    def failing_http_client(url, _timeout):
        raise HTTPError(url, 503, "Service Unavailable", hdrs=None, fp=None)

    fetcher = MetricsFetcher("http://localhost:9090", http_client=failing_http_client)

    value = fetcher.fetch_one("system_memory_usage_pct")

    assert value.value is None
    assert "503" in value.error
    assert not value.is_available


def test_metrics_fetcher_invalid_json_becomes_unavailable_metric():
    fetcher = MetricsFetcher("http://localhost:9090", http_client=lambda _u, _t: "{")

    value = fetcher.fetch_one("system_memory_usage_pct")

    assert value.value is None
    assert value.error


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="payload-not-mapping"),
        pytest.param({}, id="status-missing"),
        pytest.param({"status": "error"}, id="status-not-success"),
        pytest.param({"status": "success"}, id="data-missing"),
        pytest.param({"status": "success", "data": []}, id="data-not-mapping"),
        pytest.param(
            {"status": "success", "data": {}},
            id="result-missing",
        ),
        pytest.param(
            {"status": "success", "data": {"result": {}}},
            id="result-not-list",
        ),
        pytest.param(
            {"status": "success", "data": {"result": ["invalid"]}},
            id="first-result-not-mapping",
        ),
        pytest.param(
            {"status": "success", "data": {"result": [{"metric": {}}]}},
            id="value-missing",
        ),
        pytest.param(
            {"status": "success", "data": {"result": [{"value": [1]}]}},
            id="value-invalid-structure",
        ),
        pytest.param(
            {
                "status": "success",
                "data": {"result": [{"value": [1720000000.0, "invalid"]}]},
            },
            id="value-not-numeric",
        ),
    ],
)
def test_metrics_fetcher_malformed_response_becomes_unavailable_metric(payload):
    fetcher = MetricsFetcher(
        "http://localhost:9090", http_client=lambda _u, _t: payload
    )

    value = fetcher.fetch_one("system_memory_usage_pct")

    assert value.value is None
    assert value.error
    assert not value.is_available


@pytest.mark.parametrize(
    ("exception_type", "message"),
    [
        (AttributeError, "client bug"),
        (TypeError, "unexpected client contract"),
    ],
)
def test_metrics_fetcher_unexpected_client_error_propagates(exception_type, message):
    def failing_http_client(_url, _timeout):
        raise exception_type(message)

    fetcher = MetricsFetcher("http://localhost:9090", http_client=failing_http_client)

    with pytest.raises(exception_type, match=message):
        fetcher.fetch_one("system_memory_usage_pct")


def test_metrics_fetcher_unexpected_parser_runtime_error_propagates(monkeypatch):
    fetcher = MetricsFetcher(
        "http://localhost:9090",
        http_client=lambda _u, _t: {
            "status": "success",
            "data": {"result": []},
        },
    )

    def failing_parser(_metric_name, _queried_at, _payload):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(fetcher, "_metric_value_from_payload", failing_parser)

    with pytest.raises(RuntimeError, match="parser bug"):
        fetcher.fetch_one("system_memory_usage_pct")


@pytest.mark.parametrize(
    "value,expected",
    [
        (89.9, None),
        (90.0, "high_memory_detected"),
        (91.1, "high_memory_detected"),
    ],
)
def test_threshold_evaluator_memory_boundary(value, expected):
    result = evaluator().evaluate_one(metric("system_memory_usage_pct", value))

    assert (result.event_type if result else None) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (2999.9, None),
        (3000.0, "high_latency_detected"),
        (3000.1, "high_latency_detected"),
    ],
)
def test_threshold_evaluator_latency_boundary(value, expected):
    result = evaluator().evaluate_one(metric("api_p95_latency_ms", value))

    assert (result.event_type if result else None) == expected


def test_threshold_evaluator_ignores_unavailable_metric():
    result = evaluator().evaluate_one(
        MetricValue(
            name="system_memory_usage_pct",
            value=None,
            queried_at="2026-07-17T10:00:00.000Z",
            error="timeout",
        )
    )

    assert result is None


@pytest.mark.parametrize("metric_name", ["api_requests_per_sec", "db_pool_active_connections"])
def test_threshold_evaluator_ignores_disabled_metrics(metric_name):
    result = evaluator().evaluate_one(metric(metric_name, 999999.0))

    assert result is None


def test_threshold_evaluator_exceeded_by_and_severity_come_from_config():
    config = deepcopy(BASE_CONFIG)
    config["metrics"]["system_memory_usage_pct"]["severity"] = "CRITICAL"
    result = ThresholdEvaluator(config["metrics"]).evaluate_one(
        metric("system_memory_usage_pct", 92.5)
    )

    assert result.exceeded_by == 2.5
    assert result.severity == "CRITICAL"


def test_cooldown_manager_uses_event_type_key():
    now = 1000.0
    cooldown = CooldownManager(60, time_provider=lambda: now)

    assert cooldown.is_allowed("high_memory_detected")
    cooldown.record("high_memory_detected")
    assert not cooldown.is_allowed("high_memory_detected")
    assert cooldown.is_allowed("high_latency_detected")


def test_cooldown_manager_allows_same_event_type_after_window():
    now = 1000.0

    def time_provider():
        return now

    cooldown = CooldownManager(60, time_provider=time_provider)
    cooldown.record("high_memory_detected")
    now = 1060.0

    assert cooldown.is_allowed("high_memory_detected")


def test_event_builder_outputs_exact_prd_002_schema():
    event = builder().build(threshold_result())

    assert set(event) == EVENT_FIELDS
    assert list(event.keys()) == [
        "event_id",
        "detected_at",
        "event_source",
        "event_type",
        "detection_method",
        "severity",
        "confidence",
        "service_name",
        "trace_id",
        "source_ip",
        "downstream_service",
        "external_service",
        "status",
        "triggered_features",
        "raw_log_sample",
    ]


def test_event_builder_sets_fixed_metrics_threshold_fields():
    event = builder().build(threshold_result())

    assert event["event_id"] == "EVT-1784282401234-a3f9"
    assert event["detected_at"] == "2026-07-17T10:00:01.234Z"
    assert event["event_source"] == "metrics_threshold_detection"
    assert event["detection_method"] == "threshold"
    assert event["confidence"] == 1.0
    assert event["service_name"] == "metrics"
    assert event["trace_id"] is None
    assert event["source_ip"] is None
    assert event["downstream_service"] is None
    assert event["external_service"] is None
    assert event["status"] == "OPEN"
    assert event["raw_log_sample"] == []


def test_event_builder_keeps_extra_metrics_info_inside_triggered_features():
    event = builder().build(threshold_result())

    for forbidden in [
        "scenario_id",
        "window_start",
        "window_end",
        "root_cause",
        "alert_id",
        "incident_id",
    ]:
        assert forbidden not in event

    assert event["triggered_features"] == {
        "metric_name": "system_memory_usage_pct",
        "current_value": 92.5,
        "threshold_value": 90.0,
        "threshold_type": "upper",
        "exceeded_by": 2.5,
        "query_timestamp": 1720000000.0,
    }


class FakeFetcher:
    def __init__(self, values):
        self.values = values
        self.requested = None

    def fetch_all(self, metric_names):
        self.requested = metric_names
        return [metric(name, self.values[name]) for name in metric_names]


def detector_config(tmp_path):
    config = deepcopy(BASE_CONFIG)
    config["output"]["event_store_path"] = str(tmp_path / "unused.jsonl")
    return write_config(tmp_path, config)


def test_detector_run_once_returns_empty_for_recoverable_unavailable_metrics(tmp_path):
    def failing_http_client(_url, _timeout):
        raise TimeoutError("timeout")

    detector = MetricsThresholdDetector(
        detector_config(tmp_path),
        fetcher=MetricsFetcher(
            "http://localhost:9090", http_client=failing_http_client
        ),
        store=EventStore(tmp_path / "events.jsonl"),
    )

    assert detector.run_once() == []


def test_detector_run_once_propagates_unexpected_fetcher_error(tmp_path):
    def failing_http_client(_url, _timeout):
        raise AttributeError("client bug")

    detector = MetricsThresholdDetector(
        detector_config(tmp_path),
        fetcher=MetricsFetcher(
            "http://localhost:9090", http_client=failing_http_client
        ),
        store=EventStore(tmp_path / "events.jsonl"),
    )

    with pytest.raises(AttributeError, match="client bug"):
        detector.run_once()


def test_detector_run_once_fetches_evaluates_writes_and_returns_events(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    fake_fetcher = FakeFetcher(
        {
            "system_memory_usage_pct": 92.5,
            "api_p95_latency_ms": 3001.0,
        }
    )
    detector = MetricsThresholdDetector(
        detector_config(tmp_path),
        fetcher=fake_fetcher,
        builder=builder(),
        store=store,
    )

    events = detector.run_once()

    assert fake_fetcher.requested == [
        "api_p95_latency_ms",
        "system_memory_usage_pct",
    ] or fake_fetcher.requested == [
        "system_memory_usage_pct",
        "api_p95_latency_ms",
    ]
    assert [event["event_type"] for event in events] == [
        "high_memory_detected",
        "high_latency_detected",
    ]
    assert store.read_all() == events


def test_detector_run_once_applies_cooldown(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    fake_fetcher = FakeFetcher(
        {
            "system_memory_usage_pct": 92.5,
            "api_p95_latency_ms": 1.0,
        }
    )
    detector = MetricsThresholdDetector(
        detector_config(tmp_path),
        fetcher=fake_fetcher,
        builder=builder(),
        store=store,
    )

    first = detector.run_once()
    second = detector.run_once()

    assert len(first) == 1
    assert second == []
    assert len(store.read_all()) == 1


class FailingStore:
    def write(self, _event):
        raise OSError("write failed")


def test_detector_does_not_record_cooldown_when_write_fails(tmp_path):
    fake_fetcher = FakeFetcher(
        {
            "system_memory_usage_pct": 92.5,
            "api_p95_latency_ms": 1.0,
        }
    )
    detector = MetricsThresholdDetector(
        detector_config(tmp_path),
        fetcher=fake_fetcher,
        builder=builder(),
        store=FailingStore(),
    )

    assert detector.run_once() == []
    assert detector.cooldown.is_allowed("high_memory_detected")
