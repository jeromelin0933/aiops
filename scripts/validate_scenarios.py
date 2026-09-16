"""Controlled, append-boundary validation for the six SPEC-005 scenarios.

This program is intentionally an integration controller, not a detector.  It
only drives the public runtime / runner interfaces and reads their evidence.
In particular it never imports EventBuilder or EventStore and never writes an
event itself.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Iterable
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.event_detection.runner import EventDetectionRunner
from src.scenario_runtime import GeneratorAdapter, MockDataRuntime, ScenarioConfigLoader, ScenarioId, ScenarioPhase


EVENT_FIELDS = {
    "event_id", "detected_at", "event_source", "event_type", "detection_method",
    "severity", "confidence", "service_name", "trace_id", "source_ip",
    "downstream_service", "external_service", "status", "triggered_features",
    "raw_log_sample",
}
EXPECTED: dict[str, dict[str, tuple[str, str, str]]] = {
    "S1": {"brute_force_detected": ("log_event_detection", "isolation_forest", "CRITICAL")},
    "S2": {"cross_service_failure": ("log_event_detection", "isolation_forest", "HIGH"), "high_latency_detected": ("metrics_threshold_detection", "threshold", "HIGH")},
    "S3": {"oom_crash_detected": ("log_event_detection", "isolation_forest", "CRITICAL"), "high_memory_detected": ("metrics_threshold_detection", "threshold", "HIGH")},
    "S4": {"external_dependency_failure": ("log_event_detection", "isolation_forest", "HIGH")},
    "S5": {"downstream_cascade_failure": ("log_event_detection", "isolation_forest", "CRITICAL")},
    "S6": {"rate_limit_storm": ("log_event_detection", "isolation_forest", "HIGH"), "request_spike_detected": ("metrics_iforest_detection", "isolation_forest", "HIGH")},
}


@dataclass(frozen=True)
class ScenarioValidationResult:
    scenario_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    evidence_boundary: dict[str, int]
    input_evidence: dict[str, Any]
    observability_evidence: dict[str, Any]
    expected_events: list[str]
    actual_events: list[dict[str, Any]]
    unexpected_events: list[dict[str, Any]]
    schema_validation: dict[str, Any]
    recovery_result: dict[str, Any]
    status: str
    failure_category: str
    failure_reason: str | None


class ValidationFailure(RuntimeError):
    def __init__(self, category: str, reason: str) -> None:
        super().__init__(reason)
        self.category, self.reason = category, reason


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event_boundary(path: Path) -> dict[str, int]:
    """Return a byte boundary without truncating, creating, or opening for write."""
    return {"byte_offset": path.stat().st_size if path.exists() else 0}


def read_events_after(path: Path, boundary: dict[str, int]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(boundary["byte_offset"])
        data = handle.read().decode("utf-8")
    events: list[dict[str, Any]] = []
    for line in data.splitlines():
        if line.strip():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationFailure("EVENT_STORE", "new EventStore line is invalid JSON") from exc
            if not isinstance(value, dict):
                raise ValidationFailure("EVENT_STORE", "new EventStore line is not a JSON object")
            events.append(value)
    return events


def validate_event_schema(event: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if set(event) != EVENT_FIELDS:
        errors.append("top-level fields must exactly equal the 15-field PRD-002 schema")
    try:
        json.dumps(event, allow_nan=False)
    except (TypeError, ValueError) as exc:
        errors.append(f"event is not strict JSON serializable: {type(exc).__name__}")
    if event.get("event_source") not in {"log_event_detection", "metrics_threshold_detection", "metrics_iforest_detection"}:
        errors.append("invalid event_source")
    if event.get("detection_method") not in {"rule_based", "threshold", "isolation_forest"}:
        errors.append("invalid detection_method")
    if event.get("severity") not in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}:
        errors.append("invalid severity")
    if not isinstance(event.get("service_name"), str) or not event["service_name"].strip():
        errors.append("service_name must be non-empty")
    if event.get("event_source", "").startswith("metrics_") and event.get("raw_log_sample") != []:
        errors.append("metrics event raw_log_sample must be []")
    return errors


def _records_after(path: Path, offset: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read().decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def log_detector_checkpoint_ready(
    scenario: str,
    records: list[dict[str, Any]],
    min_log_count: int,
) -> bool:
    """Return whether this scenario has enough new evidence for Log inference."""
    valid_records = [record for record in records if isinstance(record, dict)]
    if len(valid_records) < min_log_count:
        return False
    if scenario != "S3":
        return True
    return any(
        record.get("error_type") == "OutOfMemoryError"
        and isinstance(record.get("service_name"), str)
        and bool(record["service_name"].strip())
        for record in valid_records
    )


def validate_input_evidence(scenario: str, records: list[dict[str, Any]], scenario_config: dict[str, Any]) -> tuple[bool, dict[str, Any], str | None]:
    """Validate generator output only; no expected event data is fed back to it."""
    evidence: dict[str, Any] = {"new_log_count": len(records)}
    if scenario == "S1":
        matches = [r for r in records if r.get("source_ip") == scenario_config["source_ip"] and r.get("status_code") == 401]
        timestamps = [_parse_timestamp(r.get("timestamp")) for r in matches]
        in_window = bool(timestamps) and (max(timestamps) - min(timestamps)).total_seconds() <= 60
        evidence.update(source_ip=scenario_config["source_ip"], matching_401_count=len(matches), within_60_seconds=in_window)
        valid = len(matches) == scenario_config["unauthorized_count"] == 50 and in_window
        return valid, evidence, None if valid else "S1 requires exactly 50 matching same-IP 401 logs within 60 seconds"
    if scenario == "S2":
        errors = [r for r in records if r.get("level") == "ERROR" and r.get("downstream_service") == scenario_config["downstream_service"]]
        traces = {r.get("trace_id") for r in errors if r.get("trace_id")}
        evidence.update(trace_ids=sorted(str(x) for x in traces if x), downstream_service=scenario_config["downstream_service"], error_count=len(errors))
        return len(errors) >= 3 and len(traces) == 1, evidence, None if len(errors) >= 3 and len(traces) == 1 else "S2 requires one trace with downstream ERROR chain"
    if scenario == "S3":
        matches = [r for r in records if "OutOfMemory" in str(r.get("error_type")) or "OutOfMemory" in str(r.get("error_message"))]
        evidence["oom_log_count"] = len(matches)
        return bool(matches), evidence, None if matches else "S3 requires OOM log evidence"
    if scenario == "S4":
        matches = [r for r in records if r.get("external_service") == scenario_config["external_service"] and int(r.get("status_code", 0)) >= 500]
        evidence.update(external_service=scenario_config["external_service"], matching_5xx_count=len(matches))
        return bool(matches), evidence, None if matches else "S4 requires configured external_service and 5xx"
    if scenario == "S5":
        matches = [r for r in records if r.get("downstream_service") == scenario_config["downstream_service"] and r.get("level") == "ERROR"]
        services = {r.get("service_name") for r in matches}
        timestamps = [_parse_timestamp(r.get("timestamp")) for r in matches]
        in_window = bool(timestamps) and (max(timestamps) - min(timestamps)).total_seconds() <= 60
        evidence.update(downstream_service=scenario_config["downstream_service"], affected_services=sorted(x for x in services if x), within_60_seconds=in_window)
        valid = len(services) >= 5 and in_window
        return valid, evidence, None if valid else "S5 requires five services sharing one downstream within 60 seconds"
    if scenario == "S6":
        matches = [r for r in records if r.get("target_service") == scenario_config["target_service"] and r.get("status_code") == 429]
        evidence.update(target_service=scenario_config["target_service"], matching_429_count=len(matches))
        return len(matches) == scenario_config["rate_limit_log_count"], evidence, None if len(matches) == scenario_config["rate_limit_log_count"] else "S6 requires configured 429 storm"
    return False, evidence, "unknown scenario"


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValidationFailure("GENERATOR", "log timestamp is missing or invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailure("GENERATOR", "log timestamp is not ISO8601") from exc


class ScenarioValidator:
    def __init__(self, config_path: str | Path, runner_config: str | Path, *, prometheus_url: str | None = None, loki_url: str = "http://localhost:3100/", timeout_seconds: float = 180.0, poll_seconds: float = 1.0, runtime_factory: Callable[..., MockDataRuntime] = MockDataRuntime, adapter_factory: Callable[..., GeneratorAdapter] = GeneratorAdapter, runner_factory: Callable[..., EventDetectionRunner] = EventDetectionRunner, http_get: Callable[[str, float], Any] | None = None, sleeper: Callable[[float], None] = time.sleep) -> None:
        self.config_path, self.runner_config = Path(config_path), Path(runner_config)
        self.timeout_seconds, self.poll_seconds, self.sleeper = timeout_seconds, poll_seconds, sleeper
        self.runtime_factory, self.adapter_factory, self.runner_factory = runtime_factory, adapter_factory, runner_factory
        self.http_get = http_get or self._http_get
        self.config = ScenarioConfigLoader.load(self.config_path)
        iforest = self._load_yaml(PROJECT_ROOT / "configs/metrics_iforest.yaml")
        self.prometheus_url = prometheus_url or str(iforest["prometheus"]["base_url"])
        self.loki_url = loki_url.rstrip("/")
        self.iforest = iforest
        self.event_store_path = self._resolve_store_path()
        self.log_path = self._resolve_path(self.config.raw["log"]["output_path"])
        runner = self._load_yaml(self.runner_config)
        log_config_path = runner["pipelines"]["log_event_detection"]["config_path"]
        log_config = self._load_yaml(self._resolve_path(log_config_path))
        self.log_min_log_count = log_config["window"]["min_log_count"]
        if (
            not isinstance(self.log_min_log_count, int)
            or isinstance(self.log_min_log_count, bool)
            or self.log_min_log_count <= 0
        ):
            raise ValidationFailure(
                "ENVIRONMENT", "log detector window.min_log_count must be positive"
            )

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        with path.open(encoding="utf-8") as handle:
            value = yaml.safe_load(handle)
        if not isinstance(value, dict):
            raise ValidationFailure("ENVIRONMENT", f"invalid YAML mapping: {path}")
        return value

    @staticmethod
    def _resolve_path(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else PROJECT_ROOT / path

    def _resolve_store_path(self) -> Path:
        sources = [self._load_yaml(PROJECT_ROOT / path)["output"]["event_store_path"] for path in ("configs/event_detection.yml", "configs/thresholds.yaml", "configs/metrics_iforest.yaml")]
        if len(set(sources)) != 1:
            raise ValidationFailure("ENVIRONMENT", "detector pipelines do not share an EventStore path")
        return self._resolve_path(sources[0])

    @staticmethod
    def _http_get(url: str, timeout: float) -> Any:
        with urlopen(url, timeout=timeout) as response:  # noqa: S310 - URLs are local CLI/config endpoints.
            return json.loads(response.read().decode("utf-8"))

    def prerequisites(self) -> None:
        for path in (self.config_path, self.runner_config, PROJECT_ROOT / "models/log_isolation_forest.pkl", PROJECT_ROOT / "models/metrics_isolation_forest.pkl"):
            if not path.exists():
                raise ValidationFailure("ENVIRONMENT", f"required path is missing: {path}")
        if set(self.config.scenarios) != set(ScenarioId):
            raise ValidationFailure("ENVIRONMENT", "S1 through S6 configuration is incomplete")
        try:
            self.log_path.resolve(strict=False)
            self.event_store_path.resolve(strict=False)
        except OSError as exc:
            raise ValidationFailure("ENVIRONMENT", "log or EventStore path cannot be resolved") from exc
        self._prometheus_query("up")
        try:
            with urlopen(f"{self.loki_url}/ready", timeout=5.0) as response:  # noqa: S310 - local CLI endpoint.
                if response.status != 200:
                    raise ValidationFailure("OBSERVABILITY", f"Loki readiness returned HTTP {response.status}")
        except URLError as exc:
            raise ValidationFailure("OBSERVABILITY", f"Loki unavailable: {type(exc).__name__}") from exc
        try:
            self.runner_factory(self.runner_config)
        except Exception as exc:
            raise ValidationFailure("ENVIRONMENT", f"EventDetectionRunner initialization failed: {type(exc).__name__}: {exc}") from exc

    def _prometheus_query(self, query: str, *, start: float | None = None, end: float | None = None, step: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query}
        endpoint = "/api/v1/query"
        if start is not None:
            endpoint = "/api/v1/query_range"
            params.update(start=start, end=end, step=step)
        try:
            payload = self.http_get(f"{self.prometheus_url.rstrip('/')}{endpoint}?{urlencode(params)}", float(self.iforest["prometheus"].get("timeout_seconds", 5)))
        except Exception as exc:
            raise ValidationFailure("OBSERVABILITY", f"Prometheus query failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValidationFailure("OBSERVABILITY", "Prometheus returned non-success response")
        return payload["data"]

    def _qps_samples(self) -> list[float]:
        now = time.time()
        window = self.iforest["window"]
        data = self._prometheus_query(str(self.iforest["metric"]["name"]), start=now - int(window["lookback_seconds"]), end=now, step=int(window["step_seconds"]))
        if data.get("resultType") != "matrix" or len(data.get("result", [])) != 1:
            raise ValidationFailure("OBSERVABILITY", "QPS must have exactly one Prometheus series")
        # Prometheus may repeat a value; deduplicate by sample timestamp, never
        # by value, because a stable baseline is still a valid sample history.
        values = {float(timestamp): float(value) for timestamp, value in data["result"][0].get("values", [])}
        return [values[timestamp] for timestamp in sorted(values)]

    def _s6_qps_ready(self, baseline_mean: float, current_qps: float) -> bool:
        """Return whether Prometheus has observed the formal S6 QPS spike."""
        required_qps = baseline_mean * float(self.iforest["classification"]["request_spike_ratio"])
        return current_qps >= required_qps

    def _wait(self, predicate: Callable[[], bool], description: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            self.sleeper(self.poll_seconds)
        raise ValidationFailure("TIMEOUT", f"timed out waiting for {description}")

    def _start_runtime(self) -> tuple[MockDataRuntime, threading.Event, threading.Thread]:
        adapter = self.adapter_factory(self.config)
        adapter.start()
        runtime = self.runtime_factory(self.config, tick_sink=adapter.tick)
        runtime.start()
        stopped = threading.Event()
        def loop() -> None:
            while not stopped.is_set():
                runtime.tick()
                time.sleep(self.config.tick_seconds)
        thread = threading.Thread(target=loop, name="scenario-validator-runtime", daemon=True)
        thread.start()
        self._wait(lambda: runtime.snapshot().phase is ScenarioPhase.BASELINE, "runtime BASELINE")
        return runtime, stopped, thread

    def _validate_events(self, scenario: str, events: list[dict[str, Any]], config: dict[str, Any], qps: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        schema_errors = [f"{event.get('event_id', '?')}: {error}" for event in events for error in validate_event_schema(event)]
        expected = EXPECTED[scenario]
        selected: list[dict[str, Any]] = []
        for event_type, (source, method, severity) in expected.items():
            found = next((event for event in events if event.get("event_type") == event_type and event.get("event_source") == source and event.get("detection_method") == method and event.get("severity") == severity), None)
            if found is None:
                schema_errors.append(f"missing expected {event_type} from {source}")
            else:
                selected.append(found)
        if scenario == "S1" and (not selected or selected[0].get("source_ip") != config["source_ip"]): schema_errors.append("S1 event source_ip does not match evidence")
        if scenario == "S2" and (not selected or not selected[0].get("trace_id") or selected[0].get("downstream_service") != config["downstream_service"]): schema_errors.append("S2 trace/downstream evidence mismatch")
        if scenario == "S3" and not any(e.get("event_type") == "oom_crash_detected" and e.get("severity") == "CRITICAL" for e in selected): schema_errors.append("S3 OOM severity mismatch")
        if scenario == "S4" and (not selected or selected[0].get("external_service") != config["external_service"]): schema_errors.append("S4 external_service mismatch")
        if scenario == "S5" and (not selected or selected[0].get("downstream_service") != config["downstream_service"]): schema_errors.append("S5 downstream_service mismatch")
        if scenario == "S6":
            spike = next((e for e in selected if e.get("event_type") == "request_spike_detected"), None)
            if not spike or qps.get("spike_ratio", 0) < float(self.iforest["classification"]["request_spike_ratio"]): schema_errors.append("S6 requires IForest request_spike_detected and ratio >= configured request_spike_ratio")
        unexpected = [event for event in events if event not in selected]
        return selected, unexpected, schema_errors

    def _recovery_evidence(self, scenario: str, observability: dict[str, Any]) -> dict[str, Any]:
        evidence: dict[str, Any] = {"completed": True, "runtime_phase": ScenarioPhase.BASELINE.value}
        if scenario in {"S2", "S3"}:
            metric, ceiling = ("api_p95_latency_ms", 3000.0) if scenario == "S2" else ("system_memory_usage_pct", 90.0)
            self._wait(lambda: float(self._prometheus_query(metric)["result"][0]["value"][1]) < ceiling, f"{scenario} metric recovery")
            evidence[metric] = self._prometheus_query(metric)["result"][0]["value"][1]
        elif scenario == "S6" and "baseline_mean" in observability:
            self._wait(lambda: float(self._prometheus_query("api_requests_per_sec")["result"][0]["value"][1]) < observability["baseline_mean"] * float(self.iforest["classification"]["request_spike_ratio"]), "S6 QPS recovery")
            evidence["api_requests_per_sec"] = self._prometheus_query("api_requests_per_sec")["result"][0]["value"][1]
        return evidence

    def validate(self, scenarios: Iterable[str]) -> list[ScenarioValidationResult]:
        self.prerequisites()
        runtime, stopped, thread = self._start_runtime()
        runner = self.runner_factory(self.runner_config)
        try:
            # SPEC-004 LogReader establishes its initial offset on its first read.
            # Prime the production runner before any scenario can append evidence,
            # so the first scenario cycle reads only scenario-generated records.
            prime_cycle = runner.run_once()
            if prime_cycle.failure_count:
                raise ValidationFailure(
                    "DETECTION_CONTRACT",
                    "EventDetectionRunner priming cycle contains failed pipeline",
                )

            results: list[ScenarioValidationResult] = []
            for scenario in scenarios:
                result = self._validate_one(runtime, runner, scenario)
                results.append(result)
                # A failed recovery is a hard sequence gate: do not contaminate
                # the next scenario by attempting another trigger.
                if not result.recovery_result.get("completed", False):
                    break
        finally:
            stopped.set(); runtime.stop(); thread.join(timeout=max(1.0, self.poll_seconds * 2))
        return results

    def _validate_one(self, runtime: MockDataRuntime, runner: EventDetectionRunner, scenario: str) -> ScenarioValidationResult:
        started, began = utc_now(), time.monotonic()
        boundary, log_offset = event_boundary(self.event_store_path), (self.log_path.stat().st_size if self.log_path.exists() else 0)
        config = dict(self.config.scenarios[ScenarioId(scenario)])
        input_evidence: dict[str, Any] = {}; observability: dict[str, Any] = {"prometheus_ready": True, "loki_ready": True}; events: list[dict[str, Any]] = []; unexpected: list[dict[str, Any]] = []; schema: dict[str, Any] = {"valid": False, "errors": []}; recovery: dict[str, Any] = {"completed": False}
        category, reason, triggered_ok = "NONE", None, False
        try:
            if scenario == "S6":
                self._wait(lambda: len(self._qps_samples()) >= int(self.iforest["window"]["min_sample_count"]), "S6 baseline QPS samples")
                baseline = self._qps_samples(); observability["baseline_sample_count"] = len(baseline); observability["baseline_mean"] = sum(baseline) / len(baseline)
            triggered = runtime.trigger(scenario)
            if not triggered.accepted:
                raise ValidationFailure("GENERATOR", f"trigger rejected: {triggered.reason}")
            triggered_ok = True
            self._wait(lambda: runtime.snapshot().phase is ScenarioPhase.INJECTING, f"{scenario} injection")
            # A baseline tick can race the boundary and append first.  Wait for
            # scenario-shaped input rather than treating any new log as proof.
            def has_valid_input() -> bool:
                candidate = _records_after(self.log_path, log_offset)
                return validate_input_evidence(scenario, candidate, config)[0]
            self._wait(has_valid_input, f"{scenario} generator input evidence")
            records = _records_after(self.log_path, log_offset)
            valid, input_evidence, error = validate_input_evidence(scenario, records, config)
            if not valid: raise ValidationFailure("GENERATOR", error or "generator evidence invalid")

            def log_detector_is_ready() -> bool:
                checkpoint_records = _records_after(self.log_path, log_offset)
                ready = log_detector_checkpoint_ready(
                    scenario, checkpoint_records, self.log_min_log_count
                )
                if (
                    not ready
                    and triggered_ok
                    and runtime.snapshot().phase is ScenarioPhase.BASELINE
                ):
                    raise ValidationFailure(
                        "DETECTION_CONTRACT",
                        f"{scenario} recovery completed before Log detector checkpoint",
                    )
                return ready

            self._wait(log_detector_is_ready, f"{scenario} Log detector checkpoint")
            checkpoint_records = _records_after(self.log_path, log_offset)
            input_evidence["log_detector_min_log_count"] = self.log_min_log_count
            input_evidence["log_detector_checkpoint_log_count"] = len(
                checkpoint_records
            )
            if scenario in {"S2", "S3"}:
                metric = "api_p95_latency_ms" if scenario == "S2" else "system_memory_usage_pct"; minimum = 3000.0 if scenario == "S2" else 90.0
                self._wait(lambda: float(self._prometheus_query(metric)["result"][0]["value"][1]) >= minimum, f"{scenario} Prometheus metric")
                observability[metric] = self._prometheus_query(metric)["result"][0]["value"][1]
            if scenario == "S6":
                spike_observation: dict[str, float] = {}

                def has_formal_s6_qps_spike() -> bool:
                    current = float(self._prometheus_query("api_requests_per_sec")["result"][0]["value"][1])
                    baseline_mean = float(observability["baseline_mean"])
                    if not self._s6_qps_ready(baseline_mean, current):
                        return False
                    spike_observation.update(current_qps=current, spike_ratio=current / baseline_mean)
                    return True

                self._wait(has_formal_s6_qps_spike, "S6 Prometheus QPS >= configured request_spike_ratio")
                observability.update(spike_observation)
            cycle = runner.run_once()
            if cycle.failure_count:
                raise ValidationFailure("DETECTION_CONTRACT", "EventDetectionRunner cycle contains failed pipeline")
            runner_events = [event for pipeline_result in cycle.pipeline_results for event in pipeline_result.events]
            try:
                self._wait(lambda: bool(read_events_after(self.event_store_path, boundary)), f"{scenario} EventStore append")
            except ValidationFailure as exc:
                if runner_events and exc.category == "TIMEOUT":
                    raise ValidationFailure("EVENT_STORE", "EventStore did not persist EventDetectionRunner output") from exc
                raise
            events = read_events_after(self.event_store_path, boundary)
            if any(event not in events for event in runner_events):
                raise ValidationFailure("EVENT_STORE", "EventStore evidence is missing EventDetectionRunner output")
            selected, unexpected, errors = self._validate_events(scenario, events, config, observability)
            schema = {"valid": not errors, "errors": errors}
            if errors: raise ValidationFailure("DETECTION_CONTRACT", "; ".join(errors))
            self._wait(lambda: runtime.snapshot().phase is ScenarioPhase.BASELINE, f"{scenario} recovery BASELINE")
            recovery = self._recovery_evidence(scenario, observability)
        except ValidationFailure as exc:
            category, reason = exc.category, exc.reason
        except Exception as exc:  # controller defects are separately visible in the result.
            category, reason = "VALIDATION_SCRIPT", f"{type(exc).__name__}: {exc}"
        finally:
            if triggered_ok and runtime.snapshot().phase is not ScenarioPhase.BASELINE:
                try:
                    self._wait(lambda: runtime.snapshot().phase is ScenarioPhase.BASELINE, f"{scenario} recovery BASELINE")
                    recovery = {"completed": True, "runtime_phase": runtime.snapshot().phase.value}
                except ValidationFailure as exc:
                    recovery = {"completed": False, "failure_reason": exc.reason}
                    if category == "NONE":
                        category, reason = exc.category, exc.reason
        return ScenarioValidationResult(scenario, started, utc_now(), round(time.monotonic() - began, 3), boundary, input_evidence, observability, list(EXPECTED[scenario]), events, unexpected, schema, recovery, "PASS" if category == "NONE" else "FAIL", category, reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run sequential real E2E validation for SPEC-005 scenarios")
    parser.add_argument("--config", default="configs/scenarios.yaml")
    parser.add_argument("--runner-config", default="configs/event_runner.yaml")
    parser.add_argument("--scenario", choices=list(EXPECTED))
    parser.add_argument("--all", action="store_true", help="explicitly run S1 through S6 in order")
    parser.add_argument("--prometheus-url")
    parser.add_argument("--loki-url", default="http://localhost:3100/")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)
    selected = [args.scenario] if args.scenario else list(EXPECTED)
    if args.all: selected = list(EXPECTED)
    try:
        results = ScenarioValidator(args.config, args.runner_config, prometheus_url=args.prometheus_url, loki_url=args.loki_url, timeout_seconds=args.timeout_seconds).validate(selected)
    except ValidationFailure as exc:
        print(json.dumps({"status": "FAIL", "failure_category": exc.category, "failure_reason": exc.reason}, ensure_ascii=False))
        return 2
    print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if all(result.status == "PASS" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
