"""SPEC-005 Prometheus metrics generator with exactly one series per metric."""

from __future__ import annotations

import math
from contextlib import contextmanager
from threading import RLock, Thread
from typing import Any

from prometheus_client import Gauge, start_http_server

from src.scenario_runtime.schema import ScenarioId, ScenarioPhase, ScenarioRuntimeSnapshot

METRIC_MEMORY = Gauge("system_memory_usage_pct", "Mock system memory usage percentage")
METRIC_LATENCY = Gauge("api_p95_latency_ms", "Mock API p95 latency in milliseconds")
METRIC_QPS = Gauge("api_requests_per_sec", "Mock API requests per second")
METRIC_DB_POOL = Gauge("db_pool_active_connections", "Mock active database pool connections")


class ExporterLifecycleError(RuntimeError):
    """Cleanup is incomplete; ownership must not be discarded or reported safe."""


class MetricsGenerator:
    """Update the four formal gauges; no labels or detector-facing metadata are emitted."""

    def __init__(self, config: Any) -> None:
        self._config = config
        self._exporter_started = False
        self._exporter_handles = None
        self._exporter_lock = RLock()
        self._shutdown_worker = None
        self._shutdown_error = None

    def start_exporter(self, *, address: str | None = None) -> None:
        """Retain the existing server/thread pair, leaving default bind unchanged."""
        with self._exporter_lock:
            if self._shutdown_worker is not None:
                raise ExporterLifecycleError("exporter shutdown is in progress or incomplete")
            if self._exporter_started:
                return
            port = self._config.raw["metrics"]["exporter_port"]
            handles = (start_http_server(port) if address is None
                       else start_http_server(port, addr=address))
            self._exporter_handles = handles
            self._exporter_started = True
            if not isinstance(handles, tuple) or len(handles) != 2:
                raise ExporterLifecycleError("exporter returned no owned server/thread pair")

    @property
    def exporter_running(self) -> bool:
        with self._exporter_lock:
            return self._exporter_started

    def stop_exporter(self, *, timeout_seconds: float = 2.0) -> None:
        """Bound waiting for cleanup; never claim to cancel a stuck server call.

        The helper owns exporter shutdown only, not Runtime/Domain work. A wait
        timeout retains all handles and blocks restart. No capture retry occurs.
        Normal completion requires shutdown, joined serving thread and socket
        close. This is not a hard process-exit or authoritative-I/O guarantee.
        """
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("exporter shutdown wait must be positive and finite")
        with self._exporter_lock:
            if not self._exporter_started:
                return
            if not isinstance(self._exporter_handles, tuple) or len(self._exporter_handles) != 2:
                raise ExporterLifecycleError("exporter ownership is unresolved")
            handles = self._exporter_handles
            server, serving = handles
            if self._shutdown_worker is None:
                def shutdown_owned() -> None:
                    try:
                        server.shutdown()
                        serving.join(timeout_seconds)
                        if serving.is_alive():
                            raise ExporterLifecycleError("exporter serving thread has not stopped")
                        server.server_close()
                    except BaseException as exc:
                        self._shutdown_error = exc
                self._shutdown_worker = Thread(
                    target=shutdown_owned, name="aiops-owned-exporter-shutdown", daemon=True)
                self._shutdown_worker.start()
            worker = self._shutdown_worker
        worker.join(timeout_seconds)
        with self._exporter_lock:
            # Another Stop may have completed this generation and allowed a
            # restart while we joined. Never finalize a replacement's state.
            if self._shutdown_worker is not worker or self._exporter_handles is not handles:
                return
            if worker.is_alive():
                raise ExporterLifecycleError("exporter shutdown wait expired; handles retained")
            if self._shutdown_error is not None:
                raise ExporterLifecycleError("exporter cleanup failed; handles retained") from self._shutdown_error
            if serving.is_alive():
                raise ExporterLifecycleError("exporter is still serving")
            self._exporter_handles = None
            self._shutdown_worker = None
            self._exporter_started = False

    def close(self) -> None:
        self.stop_exporter()

    @contextmanager
    def owned_exporter(self, *, address: str | None = None, timeout_seconds: float = 2.0):
        """Explicit ownership for callers; exceptions still execute cleanup."""
        self.start_exporter(address=address)
        try:
            yield self
        finally:
            self.stop_exporter(timeout_seconds=timeout_seconds)

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
