"""SPEC-005 Prometheus metrics generator with exactly one series per metric."""

from __future__ import annotations

import math
from typing import Any

from prometheus_client import Gauge, start_http_server

from src.scenario_runtime.schema import ScenarioId, ScenarioPhase, ScenarioRuntimeSnapshot

METRIC_MEMORY = Gauge("system_memory_usage_pct", "Mock system memory usage percentage")
METRIC_LATENCY = Gauge("api_p95_latency_ms", "Mock API p95 latency in milliseconds")
METRIC_QPS = Gauge("api_requests_per_sec", "Mock API requests per second")
METRIC_DB_POOL = Gauge("db_pool_active_connections", "Mock active database pool connections")


class MetricsGenerator:
    """Update the four formal gauges; no labels or detector-facing metadata are emitted."""

    def __init__(self, config: Any) -> None:
        self._config = config
        self._exporter_started = False

    def start_exporter(self) -> None:
        if not self._exporter_started:
            start_http_server(self._config.raw["metrics"]["exporter_port"])
            self._exporter_started = True

    def values_for(self, snapshot: ScenarioRuntimeSnapshot, rng: Any) -> dict[str, float]:
        baseline = self._config.raw["metrics"]["baseline"]
        jitter = self._config.raw["metrics"]["jitter"]
        def value(name: str, delta_name: str) -> float:
            delta = float(jitter[delta_name]) if jitter["enabled"] else 0.0
            return float(baseline[name]) + rng.uniform(-delta, delta)
        values = {
            "system_memory_usage_pct": value("system_memory_usage_pct", "memory_max_delta"),
            "api_p95_latency_ms": value("api_p95_latency_ms", "latency_max_delta"),
            "api_requests_per_sec": value("api_requests_per_sec", "qps_max_delta"),
            "db_pool_active_connections": value("db_pool_active_connections", "db_pool_max_delta"),
        }
        if snapshot.phase is ScenarioPhase.INJECTING and snapshot.active_scenario is not None:
            scenario = self._config.scenarios[snapshot.active_scenario]
            if snapshot.active_scenario is ScenarioId.S2:
                values["api_p95_latency_ms"] = float(scenario["api_p95_latency_ms"])
            elif snapshot.active_scenario is ScenarioId.S3:
                values["system_memory_usage_pct"] = float(scenario["system_memory_usage_pct"])
            elif snapshot.active_scenario is ScenarioId.S6:
                values["api_requests_per_sec"] = float(baseline["api_requests_per_sec"]) * float(scenario["qps_spike_multiplier"])
        return values

    def tick(self, snapshot: ScenarioRuntimeSnapshot, rng: Any) -> None:
        values = self.values_for(snapshot, rng)
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("metric values must be finite")
        METRIC_MEMORY.set(values["system_memory_usage_pct"])
        METRIC_LATENCY.set(values["api_p95_latency_ms"])
        METRIC_QPS.set(values["api_requests_per_sec"])
        METRIC_DB_POOL.set(values["db_pool_active_connections"])


def main() -> int:
    """Keep ``python -m src.metrics_generator.metrics_generator`` usable via the formal runtime."""
    from scripts.run_mock_runtime import main as runtime_main
    return runtime_main()


if __name__ == "__main__":
    raise SystemExit(main())
