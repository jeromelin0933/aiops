"""UTF-8 YAML loading and cross-config validation for scenario runtime."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Mapping

import yaml

from .schema import ScenarioId


@dataclass(frozen=True)
class ScenarioConfig:
    raw: Mapping[str, Any]
    version: str
    tick_seconds: float
    random_seed: int
    recovery_seconds: float
    scenarios: Mapping[ScenarioId, Mapping[str, Any]]

    def duration_for(self, scenario_id: ScenarioId) -> float:
        return float(self.scenarios[scenario_id]["duration_seconds"])


class ScenarioConfigLoader:
    """Load a UTF-8 YAML file and fail before any generator is started."""

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        thresholds_path: str | Path = "configs/thresholds.yaml",
        metrics_iforest_path: str | Path = "configs/metrics_iforest.yaml",
        event_detection_path: str | Path = "configs/event_detection.yml",
    ) -> ScenarioConfig:
        config_path = Path(path)
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        cls._validate_mapping(raw, "root")
        thresholds = cls._load_mapping(thresholds_path, "thresholds config")
        iforest = cls._load_mapping(metrics_iforest_path, "metrics iforest config")
        event_detection = cls._load_mapping(event_detection_path, "event detection config")
        cls._validate(raw, thresholds, iforest, event_detection)
        scenarios = {ScenarioId(key): value for key, value in raw["scenarios"].items()}
        return ScenarioConfig(
            raw=raw,
            version=raw["version"],
            tick_seconds=float(raw["runtime"]["tick_seconds"]),
            random_seed=raw["runtime"]["random_seed"],
            recovery_seconds=float(raw["runtime"]["recovery_seconds"]),
            scenarios=scenarios,
        )

    @staticmethod
    def _load_mapping(path: str | Path, label: str) -> Mapping[str, Any]:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = yaml.safe_load(handle)
        ScenarioConfigLoader._validate_mapping(value, label)
        return value

    @staticmethod
    def _validate_mapping(value: Any, label: str) -> None:
        if not isinstance(value, Mapping):
            raise ValueError(f"{label} must be a mapping")

    @staticmethod
    def _require(mapping: Mapping[str, Any], key: str, label: str) -> Any:
        if key not in mapping:
            raise ValueError(f"missing {label}.{key}")
        return mapping[key]

    @staticmethod
    def _finite(value: Any, label: str, *, minimum: float | None = None) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{label} must be a finite number")
        number = float(value)
        if minimum is not None and number < minimum:
            raise ValueError(f"{label} must be >= {minimum}")
        return number

    @staticmethod
    def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label} must be an integer")
        if minimum is not None and value < minimum:
            raise ValueError(f"{label} must be >= {minimum}")
        return value

    @staticmethod
    def _non_empty_string(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        return value

    @classmethod
    def _validate(cls, raw: Mapping[str, Any], thresholds: Mapping[str, Any], iforest: Mapping[str, Any], event_detection: Mapping[str, Any]) -> None:
        for key in ("version", "runtime", "log", "metrics", "background_errors", "scenarios", "validation"):
            cls._require(raw, key, "root")
        if not isinstance(raw["version"], str) or not raw["version"].strip():
            raise ValueError("version must be a non-empty string")
        runtime = cls._section(raw, "runtime")
        log = cls._section(raw, "log")
        metrics = cls._section(raw, "metrics")
        baseline = cls._section(metrics, "baseline")
        jitter = cls._section(metrics, "jitter")
        validation = cls._section(raw, "validation")
        background_errors = cls._section(raw, "background_errors")
        scenarios = cls._section(raw, "scenarios")

        cls._finite(cls._require(runtime, "tick_seconds", "runtime"), "runtime.tick_seconds", minimum=float.fromhex("0x1.0p-1022"))
        seed = cls._require(runtime, "random_seed", "runtime")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("runtime.random_seed must be an integer")
        recovery = cls._finite(cls._require(runtime, "recovery_seconds", "runtime"), "runtime.recovery_seconds", minimum=60)
        if runtime.get("allow_trigger_during_injection") is not False:
            raise ValueError("runtime.allow_trigger_during_injection must be false")
        if runtime.get("allow_trigger_during_recovery") is not False:
            raise ValueError("runtime.allow_trigger_during_recovery must be false")
        if not isinstance(log.get("output_path"), str) or not log["output_path"].strip():
            raise ValueError("log.output_path must be non-empty")
        cls._finite(log.get("baseline_interval_seconds"), "log.baseline_interval_seconds", minimum=float.fromhex("0x1.0p-1022"))
        records = log.get("baseline_records_per_tick")
        if isinstance(records, bool) or not isinstance(records, int) or records < 1:
            raise ValueError("log.baseline_records_per_tick must be a positive integer")
        port = metrics.get("exporter_port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("metrics.exporter_port must be between 1 and 65535")
        cls._finite(metrics.get("update_interval_seconds"), "metrics.update_interval_seconds", minimum=float.fromhex("0x1.0p-1022"))

        memory_threshold = cls._threshold(thresholds, "system_memory_usage_pct")
        latency_threshold = cls._threshold(thresholds, "api_p95_latency_ms")
        for key in ("system_memory_usage_pct", "api_p95_latency_ms", "api_requests_per_sec", "db_pool_active_connections"):
            cls._finite(baseline.get(key), f"metrics.baseline.{key}")
        if not 0 <= float(baseline["system_memory_usage_pct"]) < memory_threshold:
            raise ValueError("baseline memory must be below the formal threshold")
        if float(baseline["api_p95_latency_ms"]) >= latency_threshold:
            raise ValueError("baseline latency must be below the formal threshold")
        if float(baseline["api_requests_per_sec"]) <= 0:
            raise ValueError("baseline qps must be positive")
        if float(baseline["db_pool_active_connections"]) < 0:
            raise ValueError("baseline db pool must not be negative")
        for key in ("memory_max_delta", "latency_max_delta", "qps_max_delta", "db_pool_max_delta"):
            cls._finite(jitter.get(key), f"metrics.jitter.{key}", minimum=0)
        if jitter.get("enabled") is not True and jitter.get("enabled") is not False:
            raise ValueError("metrics.jitter.enabled must be boolean")
        if jitter["enabled"] and float(baseline["system_memory_usage_pct"]) + float(jitter["memory_max_delta"]) >= memory_threshold:
            raise ValueError("memory jitter may not cross the formal threshold")
        if jitter["enabled"] and float(baseline["api_p95_latency_ms"]) + float(jitter["latency_max_delta"]) >= latency_threshold:
            raise ValueError("latency jitter may not cross the formal threshold")
        qps_baseline = float(baseline["api_requests_per_sec"])
        qps_jitter = float(jitter["qps_max_delta"]) if jitter["enabled"] else 0.0
        if qps_baseline > 0 and (qps_baseline + qps_jitter) >= qps_baseline * 3.0:
            raise ValueError("qps jitter may not form a formal three-times spike")
        if background_errors.get("enabled") is not False:
            raise ValueError("background_errors.enabled must be false")

        if set(scenarios) != {item.value for item in ScenarioId}:
            raise ValueError("scenarios must contain exactly S1 through S6")
        for scenario_id, scenario in scenarios.items():
            cls._validate_mapping(scenario, f"scenarios.{scenario_id}")
            cls._finite(scenario.get("duration_seconds"), f"scenarios.{scenario_id}.duration_seconds", minimum=float.fromhex("0x1.0p-1022"))
        cls._integer(scenarios["S1"].get("unauthorized_count"), "S1 unauthorized_count", minimum=10)
        cls._non_empty_string(scenarios["S1"].get("source_ip"), "S1 source_ip")
        cls._non_empty_string(scenarios["S1"].get("user_id"), "S1 user_id")
        if cls._finite(scenarios["S2"].get("api_p95_latency_ms"), "S2 api_p95_latency_ms") < latency_threshold:
            raise ValueError("S2 latency must meet the formal threshold")
        cls._non_empty_string(scenarios["S2"].get("trace_id_prefix"), "S2 trace_id_prefix")
        cls._non_empty_string(scenarios["S2"].get("downstream_service"), "S2 downstream_service")
        if cls._finite(scenarios["S3"].get("system_memory_usage_pct"), "S3 system_memory_usage_pct") < memory_threshold:
            raise ValueError("S3 memory must meet the formal threshold")
        cls._non_empty_string(scenarios["S4"].get("external_service"), "S4 external_service")
        cls._integer(scenarios["S4"].get("status_code"), "S4 status_code", minimum=500)
        affected_service_count = cls._integer(scenarios["S5"].get("affected_service_count"), "S5 affected_service_count", minimum=5)
        if affected_service_count > 5:
            raise ValueError("S5 affected_service_count exceeds the formal generator service set")
        cls._non_empty_string(scenarios["S5"].get("downstream_service"), "S5 downstream_service")
        cls._non_empty_string(scenarios["S5"].get("error_type"), "S5 error_type")
        cls._integer(scenarios["S6"].get("rate_limit_log_count"), "S6 rate_limit_log_count", minimum=20)
        cls._non_empty_string(scenarios["S6"].get("target_service"), "S6 target_service")
        spike_ratio = cls._nested(iforest, "classification", "request_spike_ratio")
        if cls._finite(scenarios["S6"].get("qps_spike_multiplier"), "S6 qps_spike_multiplier") < cls._finite(spike_ratio, "metrics request_spike_ratio"):
            raise ValueError("S6 qps_spike_multiplier is below the formal spike ratio")
        derived_duration = sum(cls._finite(validation.get(key), f"validation.{key}", minimum=0) for key in ("prometheus_scrape_interval_seconds", "metrics_detection_poll_seconds", "safety_margin_seconds"))
        if float(scenarios["S2"]["duration_seconds"]) < derived_duration or float(scenarios["S3"]["duration_seconds"]) < derived_duration:
            raise ValueError("S2 and S3 duration must cover scrape, poll, and safety margin")
        if validation.get("require_qps_warmup") is not True and validation.get("require_qps_warmup") is not False:
            raise ValueError("validation.require_qps_warmup must be boolean")
        log_window = cls._finite(cls._nested(event_detection, "window", "window_seconds"), "event detection window_seconds", minimum=0)
        cooldown = max(cls._finite(cls._nested(event_detection, "event", "cooldown_seconds"), "event detection cooldown", minimum=0), cls._finite(cls._nested(thresholds, "cooldown", "seconds"), "threshold cooldown", minimum=0), cls._finite(cls._nested(iforest, "event", "cooldown_seconds"), "iforest cooldown", minimum=0))
        if recovery < max(log_window, cooldown):
            raise ValueError("runtime.recovery_seconds must cover the log window and cooldown")

    @classmethod
    def _section(cls, mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        value = cls._require(mapping, key, "config")
        cls._validate_mapping(value, key)
        return value

    @staticmethod
    def _nested(mapping: Mapping[str, Any], *keys: str) -> Any:
        value: Any = mapping
        for key in keys:
            if not isinstance(value, Mapping) or key not in value:
                raise ValueError(f"missing cross-config field {'.'.join(keys)}")
            value = value[key]
        return value

    @classmethod
    def _threshold(cls, thresholds: Mapping[str, Any], metric: str) -> float:
        return cls._finite(cls._nested(thresholds, "metrics", metric, "threshold"), f"threshold {metric}")
