from __future__ import annotations

import math
import random

import pytest

from src.metrics_generator.metrics_generator import METRIC_DB_POOL, METRIC_LATENCY, METRIC_MEMORY, METRIC_QPS, MetricsGenerator
from src.scenario_runtime import ScenarioConfigLoader, ScenarioId, ScenarioPhase
from src.scenario_runtime.schema import ScenarioRuntimeSnapshot


@pytest.fixture
def config():
    return ScenarioConfigLoader.load("configs/scenarios.yaml")


def snapshot(phase, scenario=None):
    return ScenarioRuntimeSnapshot(phase, scenario, 0.0, None, 0, False)


def test_baseline_ranges_are_safe_and_formal_metrics_are_unlabelled(config):
    generator = MetricsGenerator(config)
    values = generator.values_for(snapshot(ScenarioPhase.BASELINE), random.Random(42))
    assert 53.0 <= values["system_memory_usage_pct"] <= 57.0
    assert 200.0 <= values["api_p95_latency_ms"] <= 300.0
    assert 8.5 <= values["api_requests_per_sec"] <= 11.5
    assert 6.0 <= values["db_pool_active_connections"] <= 10.0
    assert all(not gauge._labelnames for gauge in (METRIC_MEMORY, METRIC_LATENCY, METRIC_QPS, METRIC_DB_POOL))
    assert {gauge._name for gauge in (METRIC_MEMORY, METRIC_LATENCY, METRIC_QPS, METRIC_DB_POOL)} == {
        "system_memory_usage_pct", "api_p95_latency_ms", "api_requests_per_sec", "db_pool_active_connections",
    }
    assert set(values) == {
        "system_memory_usage_pct", "api_p95_latency_ms", "api_requests_per_sec", "db_pool_active_connections",
    }
    assert all(isinstance(value, float) and math.isfinite(value) for value in values.values())


@pytest.mark.parametrize("scenario_id, metric_name, minimum", [
    (ScenarioId.S2, "api_p95_latency_ms", 3000.0),
    (ScenarioId.S3, "system_memory_usage_pct", 90.0),
])
def test_threshold_scenarios_hold_the_formal_value(config, scenario_id, metric_name, minimum):
    values = MetricsGenerator(config).values_for(snapshot(ScenarioPhase.INJECTING, scenario_id), random.Random(42))
    assert values[metric_name] >= minimum


def test_s6_qps_is_at_least_three_times_baseline_and_other_metrics_remain_baseline(config):
    generator = MetricsGenerator(config)
    baseline = generator.values_for(snapshot(ScenarioPhase.BASELINE), random.Random(42))
    values = generator.values_for(snapshot(ScenarioPhase.INJECTING, ScenarioId.S6), random.Random(42))
    assert values["api_requests_per_sec"] >= config.raw["metrics"]["baseline"]["api_requests_per_sec"] * 3.0
    assert values["system_memory_usage_pct"] < 90.0
    assert values["api_p95_latency_ms"] < 3000.0
    assert baseline["api_requests_per_sec"] < config.raw["metrics"]["baseline"]["api_requests_per_sec"] * 3.0


def test_recovery_values_return_to_baseline_ranges_and_seed_is_reproducible(config):
    generator = MetricsGenerator(config)
    first = generator.values_for(snapshot(ScenarioPhase.RECOVERY, ScenarioId.S3), random.Random(42))
    second = generator.values_for(snapshot(ScenarioPhase.BASELINE), random.Random(42))
    assert first == second
    assert first["system_memory_usage_pct"] < 90.0 and first["api_p95_latency_ms"] < 3000.0


def test_baseline_qps_samples_form_a_positive_stable_window(config):
    generator = MetricsGenerator(config)
    samples = [generator.values_for(snapshot(ScenarioPhase.BASELINE), random.Random(seed))["api_requests_per_sec"] for seed in range(12)]
    assert all(sample > 0 for sample in samples)
    assert max(samples) < config.raw["metrics"]["baseline"]["api_requests_per_sec"] * 3.0
