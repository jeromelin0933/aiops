"""Basic event-time Log Event Detection runner."""

import argparse
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import math
from pathlib import Path
import re
import time
from typing import Literal, Protocol

import yaml

from src.event_detection.event.builder import EventBuilder
from src.event_detection.log.encoder import FeatureEncoder
from src.event_detection.log.features import FeatureExtractor
from src.event_detection.log.parser import LogParser
from src.event_detection.log.reader import LogReader
from src.event_detection.log.window import WindowFeatureAggregator
from src.event_detection.model.predictor import AnomalyPredictor
from src.event_detection.model.schema import WindowSummary
from src.event_detection.store.event_store import EventStore


logger = logging.getLogger("LogEventDetectionRunner")
event_runner_logger = logging.getLogger("EventDetectionRunner")

_REDACTION_MARKER = "[REDACTED]"
_SENSITIVE_KEY = (
    r"(?:access[_-]?token|api[_-]?key|password|passwd|authorization|secret|token)"
)
_SENSITIVE_KEY_VALUE_PATTERN = re.compile(
    rf"(?P<prefix>(?<![\w-]){_SENSITIVE_KEY}(?![\w-])"
    rf"(?:\s*(?:=|:)\s*|\s+))"
    r"(?P<quote>['\"]?)"
    r"(?P<scheme>(?:Bearer|Basic)\s+)?"
    r"(?P<value>[^\s&,;'\"()\[\]{}]+)"
    r"(?P=quote)",
    re.IGNORECASE,
)
_STANDALONE_BEARER_PATTERN = re.compile(
    r"(?P<prefix>(?<![\w-])Bearer\s+)"
    r"(?P<quote>['\"]?)"
    r"(?P<value>[^\s&,;'\"()\[\]{}]+)"
    r"(?P=quote)",
    re.IGNORECASE,
)


def _sanitize_error_message(message: str) -> str:
    """Redact common credential values while retaining diagnostic context."""

    def redact_key_value(match: re.Match) -> str:
        return (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{match.group('scheme') or ''}{_REDACTION_MARKER}"
            f"{match.group('quote')}"
        )

    def redact_bearer(match: re.Match) -> str:
        return (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{_REDACTION_MARKER}{match.group('quote')}"
        )

    sanitized = _SENSITIVE_KEY_VALUE_PATTERN.sub(redact_key_value, message)
    return _STANDALONE_BEARER_PATTERN.sub(redact_bearer, sanitized)


def _normalize_runtime_error_message(exc: Exception) -> str:
    """Build a non-empty, sanitized message for a runtime pipeline failure."""
    error_type = type(exc).__name__
    try:
        message = str(exc).strip()
    except Exception:  # noqa: BLE001 - a broken __str__ must not mask pipeline failure.
        message = error_type
    if not message:
        message = error_type
    sanitized = _sanitize_error_message(message).strip()
    return sanitized or error_type


def load_config(path: str = "configs/event_detection.yml") -> dict:
    import yaml

    with Path(path).open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    return _validate_log_child_config(config)


def _validate_log_child_config(config: object) -> Mapping:
    """Validate the configuration used by the Log runtime pipeline."""
    if not isinstance(config, Mapping):
        raise TypeError("log child config root must be a mapping")

    log_reader = _optional_log_mapping(config, "log_reader")
    _optional_non_empty_string(log_reader, "log_file_path", "log_reader.log_file_path")
    _optional_finite_number(
        log_reader,
        "poll_interval_seconds",
        "log_reader.poll_interval_seconds",
        minimum=0.0,
        minimum_inclusive=False,
    )

    window = _required_log_mapping(config, "window")
    _optional_finite_number(
        window,
        "window_seconds",
        "window.window_seconds",
        minimum=0.0,
        minimum_inclusive=False,
    )
    if "min_log_count" in window:
        min_log_count = window["min_log_count"]
        if (
            not isinstance(min_log_count, int)
            or isinstance(min_log_count, bool)
            or min_log_count <= 0
        ):
            raise ValueError("window.min_log_count must be a positive integer")

    anomaly = _required_log_mapping(config, "anomaly")
    thresholds = {}
    for key in (
        "score_threshold",
        "confidence_high_threshold",
        "confidence_medium_threshold",
    ):
        field_path = f"anomaly.{key}"
        if key not in anomaly:
            raise ValueError(f"{field_path} is required")
        thresholds[key] = _finite_number(anomaly[key], field_path)
    high_threshold = thresholds["confidence_high_threshold"]
    medium_threshold = thresholds["confidence_medium_threshold"]
    if not -1.0 < high_threshold < medium_threshold < 0.0:
        raise ValueError(
            "anomaly confidence thresholds must satisfy "
            "-1 < confidence_high_threshold < confidence_medium_threshold < 0"
        )

    output = _required_log_mapping(config, "output")
    if "model_path" not in output:
        raise ValueError("output.model_path is required")
    _non_empty_string(output["model_path"], "output.model_path")
    _optional_non_empty_string(output, "event_store_path", "output.event_store_path")

    event = _optional_log_mapping(config, "event")
    _optional_finite_number(
        event,
        "cooldown_seconds",
        "event.cooldown_seconds",
        minimum=0.0,
        minimum_inclusive=True,
    )

    feature_extraction = _optional_log_mapping(config, "feature_extraction")
    for key in ("known_services", "known_error_types"):
        if key not in feature_extraction:
            continue
        field_path = f"feature_extraction.{key}"
        values = feature_extraction[key]
        if not isinstance(values, list):
            raise TypeError(f"{field_path} must be a list of strings")
        if any(not isinstance(value, str) for value in values):
            raise TypeError(f"{field_path} must contain only strings")

    return config


def _required_log_mapping(config: Mapping, key: str) -> Mapping:
    if key not in config:
        raise ValueError(f"log child config section is required: {key}")
    value = config[key]
    if not isinstance(value, Mapping):
        raise TypeError(f"log child config {key} must be a mapping")
    return value


def _optional_log_mapping(config: Mapping, key: str) -> Mapping:
    value = config.get(key, {})
    if not isinstance(value, Mapping):
        raise TypeError(f"log child config {key} must be a mapping")
    return value


def _non_empty_string(value: object, field_path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_path} must be a non-empty string")
    return value


def _optional_non_empty_string(config: Mapping, key: str, field_path: str) -> None:
    if key in config:
        _non_empty_string(config[key], field_path)


def _finite_number(value: object, field_path: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_path} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_path} must be a finite number")
    return number


def _optional_finite_number(
    config: Mapping,
    key: str,
    field_path: str,
    *,
    minimum: float,
    minimum_inclusive: bool,
) -> None:
    if key not in config:
        return
    number = _finite_number(config[key], field_path)
    valid = number >= minimum if minimum_inclusive else number > minimum
    if not valid:
        relation = "greater than or equal to" if minimum_inclusive else "greater than"
        raise ValueError(
            f"{field_path} must be a finite number {relation} {minimum:g}"
        )


class LogEventDetectionRunner:
    """Coordinate parsing, event-time window inference, and persistence."""

    def __init__(self, config_path="configs/event_detection.yml", *, config=None,
                 reader=None, predictor=None, builder=None, store=None):
        cfg = _validate_log_child_config(
            config if config is not None else load_config(config_path)
        )
        self.config = cfg
        reader_cfg = cfg.get("log_reader", {})
        self.reader = reader or LogReader(
            reader_cfg.get("log_file_path", "logs/aiops.json.log"),
            reader_cfg.get("poll_interval_seconds", 5),
        )
        self.parser = LogParser()
        self.extractor = FeatureExtractor()
        self.encoder = FeatureEncoder(cfg.get("feature_extraction", {}))
        self.aggregator = WindowFeatureAggregator(**cfg["window"])
        self.predictor = predictor or AnomalyPredictor(cfg)
        self.builder = builder or EventBuilder()
        self.store = store or EventStore(
            cfg.get("output", {}).get("event_store_path", "events/event_store.jsonl")
        )
        self.cooldown_seconds = float(cfg.get("event", {}).get("cooldown_seconds", 60))
        self._entries = []
        self._latest_event_time = None
        self._last_fired = {}
        self._initialized = False

    def initialize(self) -> None:
        """Load the prediction model exactly once before processing logs."""
        if self._initialized:
            return
        self.predictor.load()
        self._initialized = True

    def _process_raw_line(self, raw_line: str):
        """Process one line; return a persisted Event or ``None``."""
        entry = self.parser.parse(raw_line)
        if entry is None:
            return None

        # Keep Phase 1 components connected even though model inference is window-level.
        raw_features = self.extractor.extract_one(entry)
        self.encoder.encode(raw_features)
        event_time = entry["_parsed_timestamp"]
        if self._latest_event_time is None or event_time > self._latest_event_time:
            self._latest_event_time = event_time
        self._entries.append(entry)
        self._prune_window()

        if not self.aggregator.has_enough(self._entries):
            return None
        vector = self.aggregator.aggregate(
            self._entries, window_end=self._latest_event_time
        )
        prediction = self.predictor.predict_one(vector)
        if not prediction.is_anomaly:
            return None

        summary = self._compute_summary(self._entries)
        event = self.builder.build(prediction, summary)
        if event is None or self._in_cooldown(event["event_type"]):
            return None
        self.store.write(event)
        self._last_fired[event["event_type"]] = self._latest_event_time
        return event

    def process_line(self, raw_line: str):
        """Backward-compatible public wrapper for the single-line detection flow."""
        return self._process_raw_line(raw_line)

    def run_once(self) -> list[dict]:
        """Process all lines currently available from the reader without blocking."""
        self.initialize()
        events = []
        for raw_line in self.reader.read_new_lines_once():
            event = self._process_raw_line(raw_line)
            if event is not None:
                events.append(event)
        return events

    def start(self) -> None:
        """Run the standalone polling loop until interrupted."""
        self.initialize()
        logger.info("Log Event Detection started")
        try:
            while True:
                self.run_once()
                time.sleep(self.reader.poll_interval)
        except KeyboardInterrupt:
            logger.info("Log Event Detection stopped")

    def _prune_window(self):
        cutoff = self._latest_event_time - timedelta(seconds=self.aggregator.window_seconds)
        self._entries = [
            entry for entry in self._entries
            if cutoff <= entry["_parsed_timestamp"] <= self._latest_event_time
        ]

    def _in_cooldown(self, event_type):
        last = self._last_fired.get(event_type)
        return last is not None and (
            self._latest_event_time - last
        ).total_seconds() < self.cooldown_seconds

    @staticmethod
    def _compute_summary(logs):
        error_logs = [log for log in logs if str(log.get("level", "")).upper() == "ERROR"]
        warn_logs = [log for log in logs if str(log.get("level", "")).upper() == "WARN"]
        durations = [float(log.get("duration_ms") or 0) for log in logs]
        memories = [float(log.get("memory_usage_pct") or 0) for log in logs]
        ip_401 = Counter()
        target_429 = Counter()
        trace_services = defaultdict(set)
        trace_downstreams = {}
        downstream_services = defaultdict(set)
        external_failures = []
        for log in logs:
            status = int(log.get("status_code", 0))
            if status == 401 and log.get("source_ip"):
                ip_401[log["source_ip"]] += 1
            if status == 429 and log.get("target_service"):
                target_429[log["target_service"]] += 1
            if log in error_logs:
                trace_id = log.get("trace_id")
                if trace_id and log.get("service_name"):
                    trace_services[trace_id].add(log["service_name"])
                    if log.get("downstream_service"):
                        trace_downstreams.setdefault(trace_id, log["downstream_service"])
                downstream = log.get("downstream_service")
                if downstream and log.get("service_name"):
                    downstream_services[downstream].add(log["service_name"])
            if log.get("external_service") and status >= 500:
                external_failures.append(log)

        timestamps = [log["_parsed_timestamp"] for log in logs]
        samples = error_logs[:3] if error_logs else logs[:3]
        return WindowSummary(
            window_start=min(timestamps).isoformat().replace("+00:00", "Z"),
            window_end=max(timestamps).isoformat().replace("+00:00", "Z"),
            total_log_count=len(logs), error_count=len(error_logs), warn_count=len(warn_logs),
            unique_services=sorted({log.get("service_name") for log in logs if log.get("service_name")}),
            top_error_types=[name for name, _ in Counter(
                log.get("error_type") for log in logs if log.get("error_type")
            ).most_common(5)],
            max_duration_ms=max(durations, default=0),
            mean_duration_ms=sum(durations) / len(durations) if durations else 0,
            max_memory_pct=max(memories, default=0),
            source_ip_401_counts=dict(ip_401),
            trace_error_services={key: sorted(value) for key, value in trace_services.items()},
            trace_downstreams=trace_downstreams,
            downstream_error_services={key: sorted(value) for key, value in downstream_services.items()},
            target_429_counts=dict(target_429), external_failure_logs=external_failures,
            raw_log_sample=[{key: value for key, value in log.items() if not key.startswith("_")}
                            for log in samples],
        )


PIPELINE_ORDER = (
    "log_event_detection",
    "metrics_threshold_detection",
    "metrics_iforest_detection",
)

PipelineRunStatus = Literal[
    "SUCCESS",
    "FAILED",
    "SKIPPED_NOT_DUE",
]


class DetectionPipeline(Protocol):
    def run_once(self) -> list[dict]:
        ...


@dataclass
class PipelineRunResult:
    pipeline_name: str
    status: PipelineRunStatus
    started_at: str
    completed_at: str
    duration_ms: float
    events: list[dict]
    event_count: int
    error_type: str | None = None
    error_message: str | None = None
    scheduler_lag_ms: float | None = None


@dataclass
class EventRunnerCycleResult:
    mode: Literal["FORCED", "DUE_ONLY"]
    started_at: str
    completed_at: str
    pipeline_results: list[PipelineRunResult]
    total_event_count: int
    success_count: int
    failure_count: int
    skipped_count: int


@dataclass
class PipelineRuntimeState:
    name: str
    detector: DetectionPipeline
    interval_seconds: float
    next_due_monotonic: float


class EventRunnerConfigLoader:
    """Load and validate the fixed SPEC-004 orchestration configuration."""

    @staticmethod
    def load(config_path: str | Path) -> dict:
        path = Path(config_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))

        with path.open("r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
        if not isinstance(config, dict):
            raise ValueError("event runner config must be a mapping")

        EventRunnerConfigLoader._validate(config)
        return config

    @staticmethod
    def _validate(config: dict) -> None:
        runtime = config.get("runtime")
        pipelines = config.get("pipelines")
        if not isinstance(runtime, dict):
            raise ValueError("missing or invalid mapping: runtime")
        if not isinstance(pipelines, dict):
            raise ValueError("missing or invalid mapping: pipelines")

        tick_seconds = EventRunnerConfigLoader._finite_positive_number(
            runtime.get("tick_seconds"), "runtime.tick_seconds"
        )

        expected_names = set(PIPELINE_ORDER)
        actual_names = set(pipelines)
        missing_names = expected_names - actual_names
        unknown_names = actual_names - expected_names
        if missing_names:
            raise ValueError(f"missing pipeline keys: {sorted(missing_names)}")
        if unknown_names:
            raise ValueError(f"unknown pipeline keys: {sorted(unknown_names)}")

        enabled_intervals = []
        for pipeline_name in PIPELINE_ORDER:
            pipeline_config = pipelines[pipeline_name]
            if not isinstance(pipeline_config, dict):
                raise ValueError(f"pipeline config must be a mapping: {pipeline_name}")

            enabled = pipeline_config.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError(f"pipeline enabled must be boolean: {pipeline_name}")
            interval = EventRunnerConfigLoader._finite_positive_number(
                pipeline_config.get("interval_seconds"),
                f"pipelines.{pipeline_name}.interval_seconds",
            )

            if pipeline_name in {
                "metrics_threshold_detection",
                "metrics_iforest_detection",
            } and interval != 15.0:
                raise ValueError(f"metrics pipeline interval must be 15.0: {pipeline_name}")

            if not enabled:
                continue

            enabled_intervals.append(interval)
            child_config = pipeline_config.get("config_path")
            if not isinstance(child_config, str) or not child_config.strip():
                raise ValueError(f"enabled pipeline config_path is required: {pipeline_name}")
            child_path = Path(child_config)
            if not child_path.exists() or not child_path.is_file():
                raise FileNotFoundError(str(child_path))

        if not enabled_intervals:
            raise ValueError("at least one pipeline must be enabled")
        if tick_seconds > min(enabled_intervals):
            raise ValueError("runtime.tick_seconds must not exceed the minimum enabled interval")

    @staticmethod
    def _finite_positive_number(value: object, field_name: str) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"finite positive number required: {field_name}")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"finite positive number required: {field_name}")
        return number


def _create_log_pipeline(config_path: str) -> DetectionPipeline:
    detector = LogEventDetectionRunner(config_path)
    detector.initialize()
    return detector


def _create_metrics_threshold_pipeline(config_path: str) -> DetectionPipeline:
    from src.event_detection.metrics_threshold import MetricsThresholdDetector

    return MetricsThresholdDetector(config_path)


def _create_metrics_iforest_pipeline(config_path: str) -> DetectionPipeline:
    from src.event_detection.metrics_iforest import MetricsIForestDetector

    return MetricsIForestDetector(config_path)


DEFAULT_PIPELINE_FACTORIES: dict[str, Callable[[str], DetectionPipeline]] = {
    "log_event_detection": _create_log_pipeline,
    "metrics_threshold_detection": _create_metrics_threshold_pipeline,
    "metrics_iforest_detection": _create_metrics_iforest_pipeline,
}


def _validate_events_returned(pipeline_name: str, value: object) -> list[dict]:
    """Validate the minimum detector return contract without modifying Events."""
    if not isinstance(value, list):
        raise TypeError(f"{pipeline_name} run_once() must return list[dict]")
    if any(not isinstance(event, dict) for event in value):
        raise TypeError(f"{pipeline_name} run_once() returned a non-dict event")
    return value


class EventDetectionRunner:
    """Initialize the fixed set of enabled Event Detection pipelines."""

    def __init__(
        self,
        config_path: str | Path = "configs/event_runner.yaml",
        pipeline_overrides: Mapping[str, DetectionPipeline] | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.config = EventRunnerConfigLoader.load(config_path)
        event_runner_logger.info("event runner config loaded | config_path=%s", config_path)
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.tick_seconds = float(self.config["runtime"]["tick_seconds"])

        overrides = dict(pipeline_overrides or {})
        unknown_overrides = set(overrides) - set(PIPELINE_ORDER)
        if unknown_overrides:
            raise ValueError(f"unknown pipeline override names: {sorted(unknown_overrides)}")

        initialized: list[tuple[str, DetectionPipeline, float]] = []
        for pipeline_name in PIPELINE_ORDER:
            pipeline_config = self.config["pipelines"][pipeline_name]
            if not pipeline_config["enabled"]:
                continue

            detector = overrides.get(pipeline_name)
            if detector is None:
                detector = DEFAULT_PIPELINE_FACTORIES[pipeline_name](
                    pipeline_config["config_path"]
                )
            initialized.append(
                (pipeline_name, detector, float(pipeline_config["interval_seconds"]))
            )
            event_runner_logger.info(
                "event detection pipeline initialized | pipeline_name=%s",
                pipeline_name,
            )

        initial_now = self.clock()
        self.pipeline_states = {
            pipeline_name: PipelineRuntimeState(
                name=pipeline_name,
                detector=detector,
                interval_seconds=interval_seconds,
                next_due_monotonic=initial_now,
            )
            for pipeline_name, detector, interval_seconds in initialized
        }
        self._stop_requested = False
        self._ready = True

    def _execute_pipeline(
        self,
        pipeline_name: str,
        detector: DetectionPipeline,
        scheduled_due: float | None,
    ) -> PipelineRunResult:
        """Execute and isolate one pipeline after startup has completed."""
        started_at = _runner_timestamp()
        started_monotonic = self.clock()
        scheduler_lag_ms = (
            max(0.0, (started_monotonic - scheduled_due) * 1000.0)
            if scheduled_due is not None
            else None
        )

        try:
            events = _validate_events_returned(pipeline_name, detector.run_once())
        except Exception as exc:  # noqa: BLE001 - runtime isolation is the Runner contract.
            completed_monotonic = self.clock()
            completed_at = _runner_timestamp()
            error_type = type(exc).__name__
            error_message = _normalize_runtime_error_message(exc)
            event_runner_logger.error(
                "event detection pipeline failed | pipeline_name=%s | "
                "error_type=%s | error_message=%s",
                pipeline_name,
                error_type,
                error_message,
            )
            return PipelineRunResult(
                pipeline_name=pipeline_name,
                status="FAILED",
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=max(0.0, (completed_monotonic - started_monotonic) * 1000.0),
                events=[],
                event_count=0,
                error_type=error_type,
                error_message=error_message,
                scheduler_lag_ms=scheduler_lag_ms,
            )

        completed_monotonic = self.clock()
        completed_at = _runner_timestamp()
        return PipelineRunResult(
            pipeline_name=pipeline_name,
            status="SUCCESS",
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=max(0.0, (completed_monotonic - started_monotonic) * 1000.0),
            events=events,
            event_count=len(events),
            scheduler_lag_ms=scheduler_lag_ms,
        )

    def run_once(self) -> EventRunnerCycleResult:
        """Force every enabled pipeline to run once in the fixed order."""
        started_at = _runner_timestamp()
        pipeline_results = []
        for pipeline_name in PIPELINE_ORDER:
            if pipeline_name not in self.pipeline_states:
                continue
            result = self._execute_pipeline(
                pipeline_name,
                self.pipeline_states[pipeline_name].detector,
                scheduled_due=None,
            )
            pipeline_results.append(result)
            self._log_pipeline_result(result)
        completed_at = _runner_timestamp()
        cycle = self._build_cycle_result(
            "FORCED", started_at, completed_at, pipeline_results
        )
        self._log_cycle_result(cycle)
        return cycle

    def run_due_once(self) -> EventRunnerCycleResult:
        """Run only pipelines whose monotonic schedule slot is currently due."""
        started_at = _runner_timestamp()
        pipeline_results = []

        for pipeline_name in PIPELINE_ORDER:
            state = self.pipeline_states.get(pipeline_name)
            if state is None:
                continue

            now_monotonic = self.clock()
            if now_monotonic < state.next_due_monotonic:
                timestamp = _runner_timestamp()
                result = PipelineRunResult(
                    pipeline_name=pipeline_name,
                    status="SKIPPED_NOT_DUE",
                    started_at=timestamp,
                    completed_at=timestamp,
                    duration_ms=0.0,
                    events=[],
                    event_count=0,
                )
                pipeline_results.append(result)
                self._log_pipeline_result(result)
                continue

            previous_due = state.next_due_monotonic
            result = self._execute_pipeline(
                pipeline_name,
                state.detector,
                scheduled_due=previous_due,
            )
            pipeline_results.append(result)

            finished_monotonic = self.clock()
            next_due = previous_due + state.interval_seconds
            while next_due <= finished_monotonic:
                next_due += state.interval_seconds
            state.next_due_monotonic = next_due

            if result.duration_ms / 1000.0 >= state.interval_seconds:
                event_runner_logger.warning(
                    "pipeline overrun | pipeline_name=%s duration_ms=%.3f "
                    "interval_seconds=%.3f scheduler_lag_ms=%.3f",
                    pipeline_name,
                    result.duration_ms,
                    state.interval_seconds,
                    result.scheduler_lag_ms or 0.0,
                )
            self._log_pipeline_result(result)

        completed_at = _runner_timestamp()
        cycle = self._build_cycle_result(
            "DUE_ONLY", started_at, completed_at, pipeline_results
        )
        self._log_cycle_result(cycle)
        return cycle

    def start(self) -> None:
        """Run due cycles until stop is requested or the user interrupts."""
        event_runner_logger.info("event detection runner started")
        try:
            while not self._stop_requested:
                self.run_due_once()
                if self._stop_requested:
                    break
                self.sleeper(self.tick_seconds)
        except KeyboardInterrupt:
            self.stop()
            event_runner_logger.info("event detection runner interrupted")
        finally:
            event_runner_logger.info("event detection runner stopped")

    def stop(self) -> None:
        """Request a graceful stop without changing detector or Event state."""
        self._stop_requested = True

    @staticmethod
    def _build_cycle_result(
        mode: Literal["FORCED", "DUE_ONLY"],
        started_at: str,
        completed_at: str,
        pipeline_results: list[PipelineRunResult],
    ) -> EventRunnerCycleResult:
        success_count = sum(
            result.status == "SUCCESS" for result in pipeline_results
        )
        failure_count = sum(
            result.status == "FAILED" for result in pipeline_results
        )
        skipped_count = sum(
            result.status == "SKIPPED_NOT_DUE" for result in pipeline_results
        )
        total_event_count = sum(
            result.event_count
            for result in pipeline_results
            if result.status == "SUCCESS"
        )
        return EventRunnerCycleResult(
            mode=mode,
            started_at=started_at,
            completed_at=completed_at,
            pipeline_results=pipeline_results,
            total_event_count=total_event_count,
            success_count=success_count,
            failure_count=failure_count,
            skipped_count=skipped_count,
        )

    @staticmethod
    def _log_pipeline_result(result: PipelineRunResult) -> None:
        message = (
            "pipeline result | pipeline_name=%s status=%s duration_ms=%.3f "
            "event_count=%s scheduler_lag_ms=%s error_type=%s"
        )
        arguments = (
            result.pipeline_name,
            result.status,
            result.duration_ms,
            result.event_count,
            result.scheduler_lag_ms,
            result.error_type,
        )
        if result.status == "FAILED":
            event_runner_logger.error(message, *arguments)
        elif result.event_count > 0:
            event_runner_logger.warning(message, *arguments)
        else:
            event_runner_logger.debug(message, *arguments)

    @staticmethod
    def _log_cycle_result(cycle: EventRunnerCycleResult) -> None:
        event_runner_logger.info(
            "cycle summary | mode=%s success_count=%s failure_count=%s "
            "skipped_count=%s total_event_count=%s",
            cycle.mode,
            cycle.success_count,
            cycle.failure_count,
            cycle.skipped_count,
            cycle.total_event_count,
        )


def _runner_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run all enabled Event Detection pipelines")
    parser.add_argument(
        "--config",
        default="configs/event_runner.yaml",
        help="Path to the Event Runner YAML configuration",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Force all enabled pipelines to execute exactly once",
    )
    args = parser.parse_args(argv)

    try:
        runner = EventDetectionRunner(args.config)
        if args.once:
            cycle = runner.run_once()
            print(
                "Event Runner cycle completed | "
                f"mode={cycle.mode} success={cycle.success_count} "
                f"failed={cycle.failure_count} skipped={cycle.skipped_count} "
                f"events={cycle.total_event_count}"
            )
        else:
            runner.start()
    except Exception:  # noqa: BLE001 - CLI must turn fatal startup errors into non-zero exit.
        event_runner_logger.exception("event detection runner failed to start or execute")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
