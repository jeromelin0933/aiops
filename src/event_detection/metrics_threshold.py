"""Metrics threshold detection for PRD-002 events."""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import yaml

from src.event_detection.store.event_store import EventStore


logger = logging.getLogger(__name__)


class _PrometheusResponseError(ValueError):
    """A recoverable error in a Prometheus API response payload."""

EVENT_FIELDS = {
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
}

ALLOWED_THRESHOLD_EVENT_TYPES = {
    "high_memory_detected",
    "high_latency_detected",
}

ALLOWED_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}


@dataclass(frozen=True)
class MetricValue:
    name: str
    value: float | None
    queried_at: str
    query_timestamp: float | None = None
    error: str | None = None

    @property
    def is_available(self) -> bool:
        return self.value is not None and self.error is None


@dataclass(frozen=True)
class ThresholdResult:
    metric_name: str
    current_value: float
    threshold_value: float
    threshold_type: str
    exceeded_by: float
    event_type: str
    severity: str
    queried_at: str
    query_timestamp: float | None = None


class ConfigLoader:
    """Load and validate the metrics threshold configuration."""

    def __init__(self, config_path: str | Path = "configs/thresholds.yaml"):
        self.config_path = Path(config_path)

    def load(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(str(self.config_path))

        with self.config_path.open("r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)

        if not isinstance(config, dict):
            raise ValueError("threshold config must be a mapping")

        self._validate(config)
        return config

    def _validate(self, config: dict[str, Any]) -> None:
        prometheus = self._required_mapping(config, "prometheus")
        self._required_string(prometheus, "base_url")
        self._required_positive_number(prometheus, "query_timeout_seconds")

        polling = self._required_mapping(config, "polling")
        self._required_positive_number(polling, "interval_seconds")

        cooldown = self._required_mapping(config, "cooldown")
        self._required_non_negative_number(cooldown, "seconds")

        output = self._required_mapping(config, "output")
        self._required_string(output, "event_store_path")

        metrics = self._required_mapping(config, "metrics")
        enabled_count = 0
        for metric_name, rule in metrics.items():
            if not isinstance(rule, dict):
                raise ValueError(f"metric rule must be a mapping: {metric_name}")
            if not rule.get("enabled", False):
                continue

            enabled_count += 1
            self._required_string(rule, "threshold_type")
            self._required_number(rule, "threshold")
            self._required_string(rule, "event_type")
            self._required_string(rule, "severity")

            if rule["threshold_type"] != "upper":
                raise ValueError(f"unsupported threshold_type for {metric_name}")
            if rule["event_type"] not in ALLOWED_THRESHOLD_EVENT_TYPES:
                raise ValueError(f"unsupported threshold event_type for {metric_name}")
            if rule["severity"] not in ALLOWED_SEVERITIES:
                raise ValueError(f"unsupported severity for {metric_name}")

        if enabled_count == 0:
            raise ValueError("at least one metric rule must be enabled")

    @staticmethod
    def _required_mapping(config: dict[str, Any], key: str) -> dict[str, Any]:
        value = config.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"missing or invalid mapping: {key}")
        return value

    @staticmethod
    def _required_string(config: dict[str, Any], key: str) -> str:
        value = config.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"missing or invalid string: {key}")
        return value

    @staticmethod
    def _required_number(config: dict[str, Any], key: str) -> float:
        value = config.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"missing or invalid number: {key}")
        return float(value)

    @classmethod
    def _required_positive_number(cls, config: dict[str, Any], key: str) -> float:
        value = cls._required_number(config, key)
        if value <= 0:
            raise ValueError(f"number must be positive: {key}")
        return value

    @classmethod
    def _required_non_negative_number(cls, config: dict[str, Any], key: str) -> float:
        value = cls._required_number(config, key)
        if value < 0:
            raise ValueError(f"number must be non-negative: {key}")
        return value


class MetricsFetcher:
    """Fetch current metric values from the Prometheus HTTP API."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 5,
        http_client: Callable[[str, float], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client or self._default_http_client
        self.clock = clock or _utc_now

    def fetch_all(self, metric_names: list[str]) -> list[MetricValue]:
        return [self.fetch_one(metric_name) for metric_name in metric_names]

    def fetch_one(self, metric_name: str) -> MetricValue:
        queried_at = _format_iso(self.clock())
        url = f"{self.base_url}/api/v1/query?{urlencode({'query': metric_name})}"

        try:
            response = self.http_client(url, self.timeout_seconds)
        except (TimeoutError, ConnectionError, HTTPError, URLError, OSError) as exc:
            return self._unavailable_metric(metric_name, queried_at, exc)

        try:
            payload = self._parse_response(response)
            return self._metric_value_from_payload(metric_name, queried_at, payload)
        except (TimeoutError, ConnectionError, HTTPError, URLError, OSError) as exc:
            return self._unavailable_metric(metric_name, queried_at, exc)
        except (UnicodeDecodeError, json.JSONDecodeError, _PrometheusResponseError) as exc:
            return self._unavailable_metric(metric_name, queried_at, exc)

    @staticmethod
    def _unavailable_metric(
        metric_name: str, queried_at: str, error: BaseException
    ) -> MetricValue:
        return MetricValue(
            name=metric_name,
            value=None,
            queried_at=queried_at,
            error=str(error),
        )

    @staticmethod
    def _default_http_client(url: str, timeout_seconds: float) -> str:
        with urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310
            return response.read().decode("utf-8")

    @staticmethod
    def _parse_response(response: Any) -> Mapping[str, Any]:
        if isinstance(response, Mapping):
            return response
        if isinstance(response, bytes):
            response = response.decode("utf-8")
        if hasattr(response, "read"):
            response = response.read()
            if isinstance(response, bytes):
                response = response.decode("utf-8")
        if not isinstance(response, str):
            raise _PrometheusResponseError(
                "HTTP client returned unsupported response type"
            )
        payload = json.loads(response)
        if not isinstance(payload, Mapping):
            raise _PrometheusResponseError(
                "Prometheus response must be a JSON object"
            )
        return payload

    @staticmethod
    def _metric_value_from_payload(
        metric_name: str, queried_at: str, payload: Mapping[str, Any]
    ) -> MetricValue:
        if "status" not in payload:
            raise _PrometheusResponseError("Prometheus response is missing status")
        if payload["status"] != "success":
            raise _PrometheusResponseError("Prometheus response status is not success")

        if "data" not in payload:
            raise _PrometheusResponseError("Prometheus response is missing data")
        data = payload["data"]
        if not isinstance(data, Mapping):
            raise _PrometheusResponseError("Prometheus response data must be an object")

        if "result" not in data:
            raise _PrometheusResponseError("Prometheus response data is missing result")
        result = data["result"]
        if not isinstance(result, list):
            raise _PrometheusResponseError("Prometheus response result must be a list")
        if not result:
            return MetricValue(name=metric_name, value=None, queried_at=queried_at)

        first_result = result[0]
        if not isinstance(first_result, Mapping):
            raise _PrometheusResponseError(
                "Prometheus response first result must be an object"
            )
        if "value" not in first_result:
            raise _PrometheusResponseError("Prometheus result is missing value")
        value_pair = first_result["value"]
        if not isinstance(value_pair, (list, tuple)) or len(value_pair) < 2:
            raise _PrometheusResponseError("Prometheus result has invalid value")

        query_timestamp = _to_float_or_none(value_pair[0])
        try:
            value = float(value_pair[1])
        except (TypeError, ValueError, OverflowError) as exc:
            raise _PrometheusResponseError(
                "Prometheus result value must be numeric"
            ) from exc
        if not math.isfinite(value):
            return MetricValue(
                name=metric_name,
                value=None,
                queried_at=queried_at,
                query_timestamp=query_timestamp,
            )

        return MetricValue(
            name=metric_name,
            value=value,
            queried_at=queried_at,
            query_timestamp=query_timestamp,
        )


class ThresholdEvaluator:
    """Evaluate metric values against configured upper thresholds."""

    def __init__(self, metrics_config: dict[str, dict[str, Any]]):
        self.metrics_config = metrics_config

    def evaluate_all(self, metrics: list[MetricValue]) -> list[ThresholdResult]:
        results = []
        for metric in metrics:
            result = self.evaluate_one(metric)
            if result is not None:
                results.append(result)
        return results

    def evaluate_one(self, metric: MetricValue) -> ThresholdResult | None:
        rule = self.metrics_config.get(metric.name)
        if not rule or not rule.get("enabled", False) or not metric.is_available:
            return None

        threshold = float(rule["threshold"])
        current_value = float(metric.value)
        threshold_type = rule["threshold_type"]
        if threshold_type == "upper" and current_value >= threshold:
            return ThresholdResult(
                metric_name=metric.name,
                current_value=current_value,
                threshold_value=threshold,
                threshold_type=threshold_type,
                exceeded_by=current_value - threshold,
                event_type=rule["event_type"],
                severity=rule["severity"],
                queried_at=metric.queried_at,
                query_timestamp=metric.query_timestamp,
            )
        return None


class CooldownManager:
    """Suppress repeated events by event_type for the configured interval."""

    def __init__(
        self,
        cooldown_seconds: float,
        time_provider: Callable[[], float] | None = None,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.time_provider = time_provider or time.time
        self._last_fired: dict[str, float] = {}

    def is_allowed(self, event_type: str) -> bool:
        last_fired = self._last_fired.get(event_type)
        if last_fired is None:
            return True
        return self.time_provider() - last_fired >= self.cooldown_seconds

    def record(self, event_type: str) -> None:
        self._last_fired[event_type] = self.time_provider()


class MetricsThresholdEventBuilder:
    """Build PRD-002 metrics threshold events."""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        uuid_provider: Callable[[], str] | None = None,
    ):
        self.clock = clock or _utc_now
        self.uuid_provider = uuid_provider or (lambda: uuid.uuid4().hex[:4])

    def build(self, result: ThresholdResult) -> dict[str, Any]:
        event = {
            "event_id": self._make_event_id(),
            "detected_at": _format_iso(self.clock()),
            "event_source": "metrics_threshold_detection",
            "event_type": result.event_type,
            "detection_method": "threshold",
            "severity": result.severity,
            "confidence": 1.0,
            "service_name": "metrics",
            "trace_id": None,
            "source_ip": None,
            "downstream_service": None,
            "external_service": None,
            "status": "OPEN",
            "triggered_features": {
                "metric_name": result.metric_name,
                "current_value": result.current_value,
                "threshold_value": result.threshold_value,
                "threshold_type": result.threshold_type,
                "exceeded_by": result.exceeded_by,
            },
            "raw_log_sample": [],
        }
        if result.query_timestamp is not None:
            event["triggered_features"]["query_timestamp"] = result.query_timestamp

        if set(event) != EVENT_FIELDS:
            raise ValueError("metrics threshold event schema does not match PRD-002")
        return event

    def _make_event_id(self) -> str:
        timestamp = int(self.clock().timestamp() * 1000)
        return f"EVT-{timestamp}-{self.uuid_provider()}"


class MetricsThresholdDetector:
    """Coordinate fetching, evaluation, cooldown, event build, and storage."""

    def __init__(
        self,
        config_path: str | Path = "configs/thresholds.yaml",
        fetcher: MetricsFetcher | None = None,
        evaluator: ThresholdEvaluator | None = None,
        cooldown: CooldownManager | None = None,
        builder: MetricsThresholdEventBuilder | None = None,
        store: EventStore | None = None,
    ):
        self.config = ConfigLoader(config_path).load()
        self.metrics_config = self.config["metrics"]
        self.fetcher = fetcher or MetricsFetcher(
            self.config["prometheus"]["base_url"],
            self.config["prometheus"]["query_timeout_seconds"],
        )
        self.evaluator = evaluator or ThresholdEvaluator(self.metrics_config)
        self.cooldown = cooldown or CooldownManager(self.config["cooldown"]["seconds"])
        self.builder = builder or MetricsThresholdEventBuilder()
        self.store = store or EventStore(self.config["output"]["event_store_path"])

    def run_once(self) -> list[dict[str, Any]]:
        enabled_metric_names = [
            name for name, rule in self.metrics_config.items() if rule.get("enabled", False)
        ]
        metric_values = self.fetcher.fetch_all(enabled_metric_names)
        threshold_results = self.evaluator.evaluate_all(metric_values)

        events = []
        for result in threshold_results:
            if not self.cooldown.is_allowed(result.event_type):
                continue

            event = self.builder.build(result)
            try:
                self.store.write(event)
            except Exception:  # noqa: BLE001 - caller still gets successfully written events.
                logger.exception("failed to write metrics threshold event")
                continue

            self.cooldown.record(result.event_type)
            events.append(event)

        return events


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _to_float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None
