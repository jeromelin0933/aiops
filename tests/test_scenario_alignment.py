"""Cross-layer SPEC-005 contracts without external observability dependencies."""

from __future__ import annotations

import random
import inspect
from pathlib import Path
import json
from types import SimpleNamespace

import yaml
import pytest
import scripts.validate_scenarios as validation_module

from src.log_generator.log_generator import LogGenerator
from src.metrics_generator.metrics_generator import MetricsGenerator
from src.scenario_runtime import ScenarioConfigLoader, ScenarioId, ScenarioPhase
from src.scenario_runtime.schema import ScenarioRuntimeSnapshot
from scripts.validate_scenarios import (
    EVENT_FIELDS,
    EXPECTED,
    ScenarioValidator,
    ValidationFailure,
    event_boundary,
    log_detector_checkpoint_ready,
    read_events_after,
    validate_event_schema,
    validate_input_evidence,
)


FORBIDDEN_OUTPUT_FIELDS = {
    "expected_event_type", "should_trigger", "is_anomaly", "classifier_result", "scenario_id",
}


def state(phase: ScenarioPhase, scenario: ScenarioId | None = None, trigger_count: int = 0) -> ScenarioRuntimeSnapshot:
    return ScenarioRuntimeSnapshot(phase, scenario, 0.0, None, trigger_count, False)


def test_s2_and_s3_durations_cover_scrape_poll_and_safety_margin():
    config = ScenarioConfigLoader.load("configs/scenarios.yaml")
    validation = config.raw["validation"]
    required = sum(validation[name] for name in (
        "prometheus_scrape_interval_seconds", "metrics_detection_poll_seconds", "safety_margin_seconds",
    ))
    assert config.duration_for(ScenarioId.S2) >= required
    assert config.duration_for(ScenarioId.S3) >= required


def test_s6_warmup_contract_precedes_a_three_times_qps_spike():
    config = ScenarioConfigLoader.load("configs/scenarios.yaml")
    iforest = yaml.safe_load(Path("configs/metrics_iforest.yaml").read_text(encoding="utf-8"))
    generator = MetricsGenerator(config)
    warmup = [generator.values_for(state(ScenarioPhase.BASELINE), random.Random(seed))["api_requests_per_sec"] for seed in range(iforest["window"]["min_sample_count"])]
    spike = generator.values_for(state(ScenarioPhase.INJECTING, ScenarioId.S6), random.Random(42))["api_requests_per_sec"]
    assert config.raw["validation"]["require_qps_warmup"] is True
    assert len(warmup) == iforest["window"]["min_sample_count"]
    assert all(sample > 0 for sample in warmup)
    assert spike >= (sum(warmup) / len(warmup)) * iforest["classification"]["request_spike_ratio"]


def test_each_scenario_has_only_its_contractual_log_and_metric_signals(tmp_path):
    config = ScenarioConfigLoader.load("configs/scenarios.yaml")
    logs = LogGenerator(config, output_path=tmp_path / "scenario.jsonl")
    metrics = MetricsGenerator(config)
    baseline = metrics.values_for(state(ScenarioPhase.BASELINE), random.Random(42))
    for scenario_id in ScenarioId:
        records = logs.scenario_records(scenario_id, random.Random(42))
        values = metrics.values_for(state(ScenarioPhase.INJECTING, scenario_id), random.Random(42))
        assert all(not (FORBIDDEN_OUTPUT_FIELDS & record.keys()) for record in records)
        if scenario_id is ScenarioId.S2:
            assert values["api_p95_latency_ms"] >= 3000.0
        elif scenario_id is ScenarioId.S3:
            assert values["system_memory_usage_pct"] >= 90.0
        elif scenario_id is ScenarioId.S6:
            assert values["api_requests_per_sec"] >= baseline["api_requests_per_sec"] * 3.0
        else:
            assert values["system_memory_usage_pct"] < 90.0
            assert values["api_p95_latency_ms"] < 3000.0
            assert values["api_requests_per_sec"] < baseline["api_requests_per_sec"] * 3.0


def test_scenario_generators_do_not_leak_state_between_independent_triggers(tmp_path):
    config = ScenarioConfigLoader.load("configs/scenarios.yaml")
    logs = LogGenerator(config, output_path=tmp_path / "isolated.jsonl")
    s1 = logs.scenario_records(ScenarioId.S1, random.Random(42))
    s6 = logs.scenario_records(ScenarioId.S6, random.Random(42))
    recovery = MetricsGenerator(config).values_for(state(ScenarioPhase.RECOVERY, ScenarioId.S6), random.Random(42))
    assert all(record["status_code"] == 401 and record["target_service"] is None for record in s1)
    assert all(record["status_code"] == 429 and record["source_ip"] is not None for record in s6)
    assert recovery["system_memory_usage_pct"] < 90.0
    assert recovery["api_p95_latency_ms"] < 3000.0


def test_alignment_tests_use_only_tmp_output_and_no_external_service_fixture(tmp_path):
    config = ScenarioConfigLoader.load("configs/scenarios.yaml")
    output = tmp_path / "logs" / "aiops.json.log"
    generator = LogGenerator(config, output_path=output)
    generator.write_records(generator.baseline_records(random.Random(42)))
    assert output.is_file()
    assert output.parent == tmp_path / "logs"
    assert "logs/aiops.json.log" == config.raw["log"]["output_path"]


def _event(event_type, source, method, severity, **overrides):
    value = {
        "event_id": "EVT-test", "detected_at": "2026-01-01T00:00:00Z", "event_source": source,
        "event_type": event_type, "detection_method": method, "severity": severity,
        "confidence": 0.9, "service_name": "test-service", "trace_id": None,
        "source_ip": None, "downstream_service": None, "external_service": None,
        "status": "OPEN", "triggered_features": {}, "raw_log_sample": [],
    }
    value.update(overrides)
    return value


def _validator_for_event_tests():
    validator = object.__new__(ScenarioValidator)
    validator.iforest = {"metric": {"name": "api_requests_per_sec"}, "classification": {"request_spike_ratio": 3.0}}
    return validator


def test_loki_default_base_url_and_readiness_request_url(monkeypatch):
    default_loki_url = inspect.signature(ScenarioValidator).parameters["loki_url"].default
    assert default_loki_url == "http://localhost:3100/"

    requested_urls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    validator = object.__new__(ScenarioValidator)
    validator.config_path = Path("config.yaml")
    validator.runner_config = Path("runner.yaml")
    validator.config = SimpleNamespace(scenarios=list(ScenarioId))
    validator.log_path = Path("logs/aiops.json.log")
    validator.event_store_path = Path("events/events.jsonl")
    validator.loki_url = default_loki_url.rstrip("/")
    validator._prometheus_query = lambda _: None
    validator.runner_factory = lambda _: None
    monkeypatch.setattr(validation_module.Path, "exists", lambda _: True)
    monkeypatch.setattr(validation_module, "urlopen", lambda url, timeout: requested_urls.append(url) or Response())

    validator.prerequisites()

    assert requested_urls == ["http://localhost:3100/ready"]


def test_validation_boundary_does_not_reuse_old_event_or_write_store(tmp_path):
    store = tmp_path / "events.jsonl"
    old = _event("brute_force_detected", "log_event_detection", "isolation_forest", "CRITICAL")
    store.write_text(json.dumps(old) + "\n", encoding="utf-8")
    boundary = event_boundary(store)
    assert read_events_after(store, boundary) == []
    new = _event("rate_limit_storm", "log_event_detection", "isolation_forest", "HIGH")
    with store.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(new) + "\n")
    assert read_events_after(store, boundary) == [new]
    assert "from src.event_detection.store" not in Path("scripts/validate_scenarios.py").read_text(encoding="utf-8")
    assert ".write(" not in Path("scripts/validate_scenarios.py").read_text(encoding="utf-8")


def test_schema_requires_exactly_15_fields_and_metrics_empty_raw_log_sample():
    event = _event("high_latency_detected", "metrics_threshold_detection", "threshold", "HIGH")
    assert set(event) == EVENT_FIELDS
    assert validate_event_schema(event) == []
    event["raw_log_sample"] = [{"not": "allowed"}]
    event["extra"] = True
    errors = validate_event_schema(event)
    assert any("top-level" in error for error in errors)
    assert any("raw_log_sample" in error for error in errors)


def test_scenario_input_evidence_contracts_are_deterministic():
    s1 = [{"source_ip": "192.0.2.10", "status_code": 401, "timestamp": f"2026-01-01T00:00:{index:02d}Z"} for index in range(50)]
    assert validate_input_evidence("S1", s1, {"source_ip": "192.0.2.10", "unauthorized_count": 50})[0]
    assert not validate_input_evidence("S1", s1[:-1], {"source_ip": "192.0.2.10", "unauthorized_count": 50})[0]
    assert validate_input_evidence("S2", [{"level": "ERROR", "trace_id": "t", "downstream_service": "core-db"}] * 3, {"downstream_service": "core-db"})[0]
    assert validate_input_evidence("S3", [{"error_type": "OutOfMemoryError"}], {})[0]
    assert validate_input_evidence("S4", [{"external_service": "bank", "status_code": 500}], {"external_service": "bank"})[0]
    assert validate_input_evidence("S5", [{"level": "ERROR", "downstream_service": "db", "service_name": str(index), "timestamp": f"2026-01-01T00:00:0{index}Z"} for index in range(5)], {"downstream_service": "db"})[0]
    assert validate_input_evidence("S6", [{"target_service": "sms", "status_code": 429}] * 55, {"target_service": "sms", "rate_limit_log_count": 55})[0]


def test_s3_log_detector_readiness_rejects_one_log_below_configured_minimum():
    records = [{"error_type": "OutOfMemoryError", "service_name": "payment-api"}]
    assert not log_detector_checkpoint_ready("S3", records, min_log_count=5)


def test_s3_log_detector_readiness_requires_count_even_with_oom_evidence():
    records = [
        {"error_type": "OutOfMemoryError", "service_name": "payment-api"},
        {"level": "INFO", "service_name": "other-service"},
        {"level": "INFO", "service_name": "other-service"},
        {"level": "INFO", "service_name": "other-service"},
    ]
    assert not log_detector_checkpoint_ready("S3", records, min_log_count=5)


def test_s3_log_detector_readiness_accepts_enough_logs_and_valid_oom_identity():
    records = [
        {"error_type": "OutOfMemoryError", "service_name": "payment-api"},
        *[{"level": "INFO", "service_name": "other-service"} for _ in range(4)],
    ]
    assert log_detector_checkpoint_ready("S3", records, min_log_count=5)


def test_s3_log_detector_readiness_rejects_enough_logs_without_oom_evidence():
    records = [{"level": "INFO", "service_name": "payment-api"} for _ in range(5)]
    assert not log_detector_checkpoint_ready("S3", records, min_log_count=5)


def test_log_detector_readiness_uses_supplied_config_value_not_hardcoded_five():
    records = [
        {"error_type": "OutOfMemoryError", "service_name": "payment-api"},
        {"level": "INFO", "service_name": "other-service"},
        {"level": "INFO", "service_name": "other-service"},
    ]
    assert log_detector_checkpoint_ready("S3", records, min_log_count=3)
    assert not log_detector_checkpoint_ready("S3", records, min_log_count=4)


def test_log_detector_readiness_timeout_is_bounded_without_real_sleep(monkeypatch):
    validator = object.__new__(ScenarioValidator)
    validator.timeout_seconds = 1.0
    validator.poll_seconds = 0.0
    validator.sleeper = lambda _: None
    values = iter([0.0, 0.5, 2.0])
    monkeypatch.setattr(validation_module.time, "monotonic", lambda: next(values))
    with pytest.raises(ValidationFailure, match="timed out") as error:
        validator._wait(
            lambda: log_detector_checkpoint_ready("S3", [], min_log_count=7),
            "S3 Log detector checkpoint",
        )
    assert error.value.category == "TIMEOUT"


def test_dual_source_contracts_allow_both_events_and_keep_non_matching_events_unexpected():
    validator = _validator_for_event_tests()
    s2 = [
        _event("cross_service_failure", "log_event_detection", "isolation_forest", "HIGH", trace_id="trace-s2", downstream_service="core-db"),
        _event("high_latency_detected", "metrics_threshold_detection", "threshold", "HIGH"),
    ]
    selected, unexpected, errors = validator._validate_events("S2", s2, {"downstream_service": "core-db"}, {})
    assert len(selected) == 2 and unexpected == [] and errors == []
    s3 = [
        _event("oom_crash_detected", "log_event_detection", "isolation_forest", "CRITICAL"),
        _event("high_memory_detected", "metrics_threshold_detection", "threshold", "HIGH"),
    ]
    assert validator._validate_events("S3", s3, {}, {})[2] == []


def test_s4_s5_and_s6_event_specific_gates():
    validator = _validator_for_event_tests()
    assert validator._validate_events("S4", [_event("external_dependency_failure", "log_event_detection", "isolation_forest", "HIGH", external_service="bank")], {"external_service": "bank"}, {})[2] == []
    assert validator._validate_events("S5", [_event("downstream_cascade_failure", "log_event_detection", "isolation_forest", "CRITICAL", downstream_service="core-db")], {"downstream_service": "core-db"}, {})[2] == []
    base = [_event("rate_limit_storm", "log_event_detection", "isolation_forest", "HIGH"), _event("request_spike_detected", "metrics_iforest_detection", "isolation_forest", "HIGH")]
    assert validator._validate_events("S6", base, {}, {"spike_ratio": 3.0})[2] == []
    assert validator._validate_events("S6", base, {}, {"spike_ratio": 2.9})[2]
    normal = [base[0], _event("general_metrics_anomaly", "metrics_iforest_detection", "isolation_forest", "MEDIUM")]
    assert validator._validate_events("S6", normal, {}, {"spike_ratio": 4.0})[2]


def test_expected_matrix_is_sequential_contract_and_has_all_six_scenarios():
    assert list(EXPECTED) == ["S1", "S2", "S3", "S4", "S5", "S6"]
    assert [len(EXPECTED[scenario]) for scenario in EXPECTED] == [1, 2, 2, 1, 1, 2]
    source = Path("scripts/validate_scenarios.py").read_text(encoding="utf-8")
    assert "for scenario in scenarios:" in source
    assert "ScenarioPhase.BASELINE" in source
    assert "if not result.recovery_result.get" in source
    assert '"TIMEOUT"' in source


def test_s6_baseline_readiness_counts_distinct_timestamps_not_distinct_values():
    validator = _validator_for_event_tests()
    validator.iforest.update({"window": {"lookback_seconds": 300, "step_seconds": 15}})
    validator._prometheus_query = lambda *args, **kwargs: {
        "resultType": "matrix", "result": [{"values": [[1, "10"], [2, "10"], [3, "10"]]}]
    }
    assert validator._qps_samples() == [10.0, 10.0, 10.0]


@pytest.mark.parametrize(
    ("baseline_mean", "current_qps", "expected"),
    [
        (10.0, 11.0, False),  # Normal jitter is not S6 readiness.
        (10.0, 29.9, False),  # Below the formal 3x boundary is not readiness.
        (10.0, 30.0, True),   # The formal boundary is readiness.
        (10.0, 40.0, True),   # Values above the boundary are readiness.
    ],
)
def test_s6_prometheus_readiness_requires_configured_request_spike_ratio(baseline_mean, current_qps, expected):
    validator = _validator_for_event_tests()
    assert validator._s6_qps_ready(baseline_mean, current_qps) is expected


def test_s6_prometheus_readiness_uses_configured_ratio_not_hardcoded_value():
    validator = _validator_for_event_tests()
    validator.iforest["classification"]["request_spike_ratio"] = 4.0
    assert not validator._s6_qps_ready(10.0, 30.0)
    assert validator._s6_qps_ready(10.0, 40.0)


def test_s6_runner_waits_for_formal_prometheus_readiness_before_running(monkeypatch, tmp_path):
    store = tmp_path / "events.jsonl"
    order: list[str] = []
    phase = {"value": ScenarioPhase.BASELINE}
    events = [
        _event("rate_limit_storm", "log_event_detection", "isolation_forest", "HIGH"),
        _event("request_spike_detected", "metrics_iforest_detection", "isolation_forest", "HIGH"),
    ]

    class Runner:
        def run_once(self):
            order.append("scenario run_once")
            with store.open("a", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event) + "\n")
            phase["value"] = ScenarioPhase.BASELINE
            return SimpleNamespace(pipeline_results=[SimpleNamespace(events=events)], failure_count=0)

    def trigger(scenario):
        order.append(f"trigger {scenario}")
        phase["value"] = ScenarioPhase.INJECTING
        return SimpleNamespace(accepted=True, reason=None)

    runtime = SimpleNamespace(
        trigger=trigger,
        snapshot=lambda: state(phase["value"]),
    )
    validator = object.__new__(ScenarioValidator)
    validator.event_store_path = store
    validator.log_path = tmp_path / "aiops.json.log"
    validator.config = SimpleNamespace(scenarios={ScenarioId.S6: {"target_service": "sms-gateway", "rate_limit_log_count": 55}})
    validator.log_min_log_count = 5
    validator.iforest = {"classification": {"request_spike_ratio": 3.0}, "window": {"min_sample_count": 3}}
    validator._qps_samples = lambda: [10.0, 10.0, 10.0]
    qps_values = iter([11.0, 40.0, 10.0, 10.0])
    validator._prometheus_query = lambda _: {"result": [{"value": [0, str(next(qps_values))]}]}

    def wait(condition, description):
        if description == "S6 Prometheus QPS >= configured request_spike_ratio":
            assert not condition(), "baseline jitter must not allow runner.run_once()"
            assert "scenario run_once" not in order
            assert condition(), "runner must wait for the formal configured-ratio readiness boundary"
            return
        assert condition()

    validator._wait = wait
    monkeypatch.setattr(validation_module, "_records_after", lambda *_: [{"target_service": "sms-gateway", "status_code": 429}] * 55)

    result = validator._validate_one(runtime, Runner(), "S6")

    assert result.status == "PASS"
    assert result.observability_evidence["current_qps"] == 40.0
    assert result.observability_evidence["spike_ratio"] == 4.0
    assert order.index("trigger S6") < order.index("scenario run_once")


def test_timeout_is_explicit_failure_category_without_real_sleep(monkeypatch):
    validator = object.__new__(ScenarioValidator)
    validator.timeout_seconds = 1.0
    validator.poll_seconds = 0.0
    validator.sleeper = lambda _: None
    values = iter([0.0, 2.0])
    monkeypatch.setattr(validation_module.time, "monotonic", lambda: next(values))
    with pytest.raises(ValidationFailure, match="timed out") as error:
        validator._wait(lambda: False, "fake condition")
    assert error.value.category == "TIMEOUT"


def test_validator_primes_runner_before_scenario_boundary_trigger_and_evidence(monkeypatch, tmp_path):
    store = tmp_path / "events.jsonl"
    priming_event = _event("baseline_event", "log_event_detection", "isolation_forest", "LOW", event_id="EVT-prime")
    scenario_event = _event("brute_force_detected", "log_event_detection", "isolation_forest", "CRITICAL", event_id="EVT-s1", source_ip="192.0.2.10")
    order: list[str] = []
    priming_boundary: int | None = None

    def append(event):
        with store.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def cycle(events):
        return SimpleNamespace(
            pipeline_results=[SimpleNamespace(events=events)],
            failure_count=0,
        )

    class Runner:
        calls = 0

        def run_once(self):
            nonlocal priming_boundary
            self.calls += 1
            if self.calls == 1:
                order.append("prime run_once")
                append(priming_event)
                priming_boundary = store.stat().st_size
                return cycle([priming_event])
            order.append("scenario run_once")
            append(scenario_event)
            return cycle([scenario_event])

    class Stopped:
        def set(self):
            order.append("stopped")

    class Thread:
        def join(self, **_):
            order.append("joined")

    runtime = SimpleNamespace(
        trigger=lambda scenario: order.append(f"trigger {scenario}") or SimpleNamespace(accepted=True, reason=None),
        snapshot=lambda: state(ScenarioPhase.BASELINE),
        stop=lambda: order.append("runtime stopped"),
    )
    validator = object.__new__(ScenarioValidator)
    validator.runner_config = Path("runner.yaml")
    validator.event_store_path = store
    validator.log_path = tmp_path / "aiops.json.log"
    validator.config = SimpleNamespace(scenarios={ScenarioId.S1: {"source_ip": "192.0.2.10", "unauthorized_count": 50}})
    validator.log_min_log_count = 5
    validator.iforest = {"classification": {"request_spike_ratio": 3.0}}
    validator.poll_seconds = 0.0
    validator.prerequisites = lambda: order.append("prerequisites")
    validator._start_runtime = lambda: (runtime, Stopped(), Thread())
    validator.runner_factory = lambda _: Runner()
    validator._wait = lambda condition, _description: condition()
    monkeypatch.setattr(validation_module, "_records_after", lambda *_: [
        {"source_ip": "192.0.2.10", "status_code": 401, "timestamp": f"2026-01-01T00:00:{index:02d}Z"}
        for index in range(50)
    ])
    real_boundary = validation_module.event_boundary

    def record_boundary(path):
        order.append("scenario boundary")
        return real_boundary(path)

    monkeypatch.setattr(validation_module, "event_boundary", record_boundary)

    results = validator.validate(["S1"])

    assert order.index("prime run_once") < order.index("scenario boundary") < order.index("trigger S1") < order.index("scenario run_once")
    assert results[0].evidence_boundary == {"byte_offset": priming_boundary}
    assert results[0].actual_events == [scenario_event]
    assert priming_event not in results[0].actual_events


def test_validator_stops_before_trigger_when_priming_cycle_has_failed_pipeline():
    order: list[str] = []
    runner = SimpleNamespace(run_once=lambda: order.append("prime run_once") or SimpleNamespace(failure_count=1))
    runtime = SimpleNamespace(trigger=lambda scenario: order.append(f"trigger {scenario}"), stop=lambda: order.append("runtime stopped"))
    validator = object.__new__(ScenarioValidator)
    validator.runner_config = Path("runner.yaml")
    validator.poll_seconds = 0.0
    validator.prerequisites = lambda: None
    validator._start_runtime = lambda: (runtime, SimpleNamespace(set=lambda: order.append("stopped")), SimpleNamespace(join=lambda **_: order.append("joined")))
    validator.runner_factory = lambda _: runner
    validator._validate_one = lambda *_: pytest.fail("scenario validation must not run after a failed priming cycle")

    with pytest.raises(ValidationFailure, match="priming cycle contains failed pipeline") as error:
        validator.validate(["S1"])

    assert error.value.category == "DETECTION_CONTRACT"
    assert order == ["prime run_once", "stopped", "runtime stopped", "joined"]


def test_validator_uses_formal_cycle_failure_count_and_event_store_evidence(monkeypatch, tmp_path):
    event = _event("brute_force_detected", "log_event_detection", "isolation_forest", "CRITICAL", source_ip="192.0.2.10")

    def cycle(failure_count):
        runner_events = [event] if failure_count == 0 else []
        return SimpleNamespace(
            mode="FORCED",
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            pipeline_results=[SimpleNamespace(events=runner_events)],
            total_event_count=len(runner_events),
            success_count=1 - failure_count,
            failure_count=failure_count,
            skipped_count=0,
        )

    def validate(case, initial_events, persisted_events, failure_count=0):
        store = tmp_path / f"{case}.jsonl"
        if initial_events:
            store.write_text("".join(json.dumps(value) + "\n" for value in initial_events), encoding="utf-8")
        validator = object.__new__(ScenarioValidator)
        validator.event_store_path = store
        validator.log_path = tmp_path / "aiops.json.log"
        validator.config = SimpleNamespace(scenarios={ScenarioId.S1: {"source_ip": "192.0.2.10", "unauthorized_count": 50}})
        validator.log_min_log_count = 5
        validator.iforest = {"classification": {"request_spike_ratio": 3.0}}
        validator._wait = lambda condition, description: None

        def run_once():
            if persisted_events:
                with store.open("a", encoding="utf-8") as handle:
                    for value in persisted_events:
                        handle.write(json.dumps(value) + "\n")
            return cycle(failure_count)

        return validator._validate_one(runtime, SimpleNamespace(run_once=run_once), "S1")

    runtime = SimpleNamespace(
        trigger=lambda _: SimpleNamespace(accepted=True, reason=None),
        snapshot=lambda: state(ScenarioPhase.BASELINE),
    )
    records = [{"source_ip": "192.0.2.10", "status_code": 401, "timestamp": f"2026-01-01T00:00:{index:02d}Z"} for index in range(50)]
    monkeypatch.setattr(validation_module, "_records_after", lambda *_: records)

    failed_pipeline = validate("failed-pipeline", [], [], failure_count=1)
    assert failed_pipeline.status == "FAIL"
    assert failed_pipeline.failure_category == "DETECTION_CONTRACT"

    missing_persisted = validate("missing-persisted", [], [{**event, "event_id": "EVT-not-runner-output"}])
    assert missing_persisted.status == "FAIL"
    assert missing_persisted.failure_category == "EVENT_STORE"

    old_event_only = validate("old-event-only", [event], [])
    assert old_event_only.status == "FAIL"
    assert old_event_only.failure_category == "EVENT_STORE"

    passed = validate("persisted-runner-event", [], [event])
    assert passed.status == "PASS"
    assert passed.actual_events == [event]
