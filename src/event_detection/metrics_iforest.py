"""QPS window ingestion and feature extraction for SPEC-003.

This module deliberately stops before model training, prediction, event building,
cooldown management, and the detector runtime loop.  Those responsibilities are
implemented in later SPEC-003 phases.
"""

from __future__ import annotations

import logging
import math
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, Callable

import joblib
import numpy as np
import requests
import sklearn
import yaml
from sklearn.ensemble import IsolationForest

from src.event_detection.store.event_store import EventStore


logger = logging.getLogger(__name__)

QPS_METRIC_NAME = "api_requests_per_sec"
QUERY_RANGE_ENDPOINT = "/api/v1/query_range"
KNOWN_EVENT_TYPE = "request_spike_detected"
FALLBACK_EVENT_TYPE = "general_metrics_anomaly"
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


class MetricsIForestError(Exception):
    """Base exception for Metrics Isolation Forest work."""


class MetricsIForestModelNotFoundError(MetricsIForestError):
    """Raised later when an expected model artifact is absent."""


class MetricsIForestModelLoadError(MetricsIForestError):
    """Raised later when a model artifact cannot be loaded."""


class MetricsIForestModelVersionError(MetricsIForestError):
    """Raised later when model metadata does not meet the contract."""


class MetricsIForestTrainingDataError(MetricsIForestError):
    """Raised later when fixed baseline data is invalid or insufficient."""


@dataclass(frozen=True)
class MetricSample:
    timestamp: float
    value: float


@dataclass(frozen=True)
class MetricWindow:
    metric_name: str
    start_timestamp: float
    end_timestamp: float
    samples: list[MetricSample]


@dataclass(frozen=True)
class MetricsWindowFeatures:
    current_value: float
    mean_value: float
    std_value: float
    min_value: float
    max_value: float
    median_value: float
    first_value: float
    last_value: float
    max_to_mean_ratio: float
    current_to_mean_ratio: float
    slope: float
    sample_count: int

    def to_list(self) -> list[float]:
        return [
            self.current_value,
            self.mean_value,
            self.std_value,
            self.min_value,
            self.max_value,
            self.median_value,
            self.first_value,
            self.last_value,
            self.max_to_mean_ratio,
            self.current_to_mean_ratio,
            self.slope,
            float(self.sample_count),
        ]

    @staticmethod
    def feature_names() -> list[str]:
        return [
            "current_value",
            "mean_value",
            "std_value",
            "min_value",
            "max_value",
            "median_value",
            "first_value",
            "last_value",
            "max_to_mean_ratio",
            "current_to_mean_ratio",
            "slope",
            "sample_count",
        ]


@dataclass(frozen=True)
class MetricsPredictionResult:
    is_anomaly: bool
    model_label: int
    anomaly_score: float
    confidence: float
    features: MetricsWindowFeatures


@dataclass(frozen=True)
class MetricsClassificationResult:
    event_type: str
    severity: str
    classification_reason: str
    baseline_mean: float
    spike_ratio: float | None
    baseline_zero: bool


class MetricsIForestConfigLoader:
    """Read and validate the complete SPEC-003 configuration contract."""

    @staticmethod
    def load(config_path: str | Path) -> dict[str, Any]:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(str(path))

        with path.open("r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
        if not isinstance(config, dict):
            raise ValueError("metrics iforest config must be a mapping")

        MetricsIForestConfigLoader._validate(config)
        return config

    @classmethod
    def _validate(cls, config: dict[str, Any]) -> None:
        prometheus = cls._mapping(config, "prometheus")
        cls._string(prometheus, "base_url")
        if prometheus.get("query_endpoint") != QUERY_RANGE_ENDPOINT:
            raise ValueError("prometheus.query_endpoint must be /api/v1/query_range")
        cls._positive_number(prometheus, "timeout_seconds")

        metric = cls._mapping(config, "metric")
        if metric.get("name") != QPS_METRIC_NAME:
            raise ValueError("metric.name must be api_requests_per_sec")

        window = cls._mapping(config, "window")
        lookback = cls._positive_number(window, "lookback_seconds")
        step = cls._positive_number(window, "step_seconds")
        min_samples = cls._number(window, "min_sample_count")
        if not min_samples.is_integer() or min_samples <= 1:
            raise ValueError("window.min_sample_count must be an integer greater than 1")
        if lookback % step != 0:
            raise ValueError("window.lookback_seconds must be divisible by step_seconds")
        if lookback / step + 1 < min_samples:
            raise ValueError("expected sample count must meet min_sample_count")

        runtime = cls._mapping(config, "runtime")
        cls._positive_number(runtime, "poll_interval_seconds")

        forest = cls._mapping(config, "isolation_forest")
        contamination = cls._number(forest, "contamination")
        if not 0 < contamination < 0.5:
            raise ValueError("isolation_forest.contamination must be between 0 and 0.5")
        estimators = cls._number(forest, "n_estimators")
        if not estimators.is_integer() or estimators <= 0:
            raise ValueError("isolation_forest.n_estimators must be a positive integer")

        anomaly = cls._mapping(config, "anomaly")
        score_threshold = cls._number(anomaly, "score_threshold")
        medium_score = cls._number(anomaly, "confidence_medium_score")
        high_score = cls._number(anomaly, "confidence_high_score")
        if score_threshold > 0:
            raise ValueError("anomaly.score_threshold must be less than or equal to zero")
        if not high_score < medium_score < 0:
            raise ValueError("confidence scores must satisfy high < medium < 0")

        classification = cls._mapping(config, "classification")
        if cls._number(classification, "request_spike_ratio") <= 1.0:
            raise ValueError("classification.request_spike_ratio must be greater than 1")
        if classification.get("known_event_type") != KNOWN_EVENT_TYPE:
            raise ValueError("classification.known_event_type is invalid")
        if classification.get("fallback_event_type") != FALLBACK_EVENT_TYPE:
            raise ValueError("classification.fallback_event_type is invalid")

        event = cls._mapping(config, "event")
        cls._non_negative_number(event, "cooldown_seconds")

        model = cls._mapping(config, "model")
        cls._string(model, "path")
        cls._string(model, "metadata_version")
        if not isinstance(model.get("train_if_missing"), bool):
            raise ValueError("model.train_if_missing must be a boolean")

        training = cls._mapping(config, "training")
        cls._string(training, "baseline_fixture_path")
        minimum_windows = cls._number(training, "minimum_window_count")
        if not minimum_windows.is_integer() or minimum_windows < 10:
            raise ValueError("training.minimum_window_count must be an integer of at least 10")

        output = cls._mapping(config, "output")
        cls._string(output, "event_store_path")

    @staticmethod
    def _mapping(config: dict[str, Any], name: str) -> dict[str, Any]:
        value = config.get(name)
        if not isinstance(value, dict):
            raise ValueError(f"missing or invalid mapping: {name}")
        return value

    @staticmethod
    def _string(config: dict[str, Any], name: str) -> str:
        value = config.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"missing or invalid string: {name}")
        return value

    @staticmethod
    def _number(config: dict[str, Any], name: str) -> float:
        value = config.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"missing or invalid number: {name}")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"number must be finite: {name}")
        return number

    @classmethod
    def _positive_number(cls, config: dict[str, Any], name: str) -> float:
        value = cls._number(config, name)
        if value <= 0:
            raise ValueError(f"number must be positive: {name}")
        return value

    @classmethod
    def _non_negative_number(cls, config: dict[str, Any], name: str) -> float:
        value = cls._number(config, name)
        if value < 0:
            raise ValueError(f"number must be non-negative: {name}")
        return value


class PrometheusRangeClient:
    """Fetch and normalize a single QPS series from Prometheus query_range."""

    def __init__(self, base_url: str, endpoint: str, timeout_seconds: float):
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    def fetch_samples(
        self,
        metric_name: str,
        start_timestamp: float,
        end_timestamp: float,
        step_seconds: int,
    ) -> list[MetricSample]:
        url = f"{self.base_url}{self.endpoint}"
        params = {
            "query": metric_name,
            "start": start_timestamp,
            "end": end_timestamp,
            "step": step_seconds,
        }
        try:
            response = requests.get(url, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("Prometheus query_range request failed: %s", exc)
            return []

        return self._parse_matrix(payload)

    @staticmethod
    def _parse_matrix(payload: Any) -> list[MetricSample]:
        if not isinstance(payload, dict) or payload.get("status") != "success":
            logger.error("Prometheus query_range returned an unsuccessful response")
            return []

        data = payload.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "matrix":
            logger.error("Prometheus query_range response must have matrix resultType")
            return []

        result = data.get("result")
        if not isinstance(result, list):
            logger.error("Prometheus query_range result must be a list")
            return []
        if not result:
            return []
        if len(result) != 1:
            logger.error("Prometheus query_range returned %s series; expected exactly one", len(result))
            return []

        series = result[0]
        if not isinstance(series, dict) or not isinstance(series.get("values"), list):
            logger.error("Prometheus query_range series has invalid values")
            return []

        by_timestamp: dict[float, MetricSample] = {}
        for raw_sample in series["values"]:
            sample = PrometheusRangeClient._parse_sample(raw_sample)
            if sample is not None:
                by_timestamp[sample.timestamp] = sample

        return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]

    @staticmethod
    def _parse_sample(raw_sample: Any) -> MetricSample | None:
        if not isinstance(raw_sample, (list, tuple)) or len(raw_sample) < 2:
            logger.debug("Skipping malformed Prometheus sample")
            return None
        try:
            timestamp = float(raw_sample[0])
            value = float(raw_sample[1])
        except (TypeError, ValueError):
            logger.debug("Skipping non-numeric Prometheus sample")
            return None
        if not math.isfinite(timestamp) or not math.isfinite(value) or value < 0:
            logger.debug("Skipping non-finite or negative QPS sample")
            return None
        return MetricSample(timestamp=timestamp, value=value)


class MetricWindowBuilder:
    """Construct a QPS window only when enough valid samples are available."""

    def build(
        self,
        metric_name: str,
        samples: list[MetricSample],
        min_sample_count: int,
    ) -> MetricWindow | None:
        if metric_name != QPS_METRIC_NAME:
            raise ValueError("MetricWindow only supports api_requests_per_sec")
        if min_sample_count <= 1:
            raise ValueError("min_sample_count must be greater than 1")

        ordered_samples = sorted(samples, key=lambda sample: sample.timestamp)
        if len(ordered_samples) < min_sample_count:
            return None
        return MetricWindow(
            metric_name=metric_name,
            start_timestamp=ordered_samples[0].timestamp,
            end_timestamp=ordered_samples[-1].timestamp,
            samples=ordered_samples,
        )


class MetricsFeatureExtractor:
    """Extract the immutable 12-feature SPEC-003 model contract."""

    def extract(self, window: MetricWindow) -> MetricsWindowFeatures:
        if window.metric_name != QPS_METRIC_NAME or not window.samples:
            raise ValueError("a non-empty api_requests_per_sec window is required")

        values = np.asarray([sample.value for sample in window.samples], dtype=float)
        if not np.all(np.isfinite(values)):
            raise ValueError("window samples must contain only finite values")

        mean_value = float(np.mean(values))
        max_value = float(np.max(values))
        current_value = float(values[-1])
        sample_count = int(values.size)
        if mean_value > 0:
            max_to_mean_ratio = max_value / mean_value
            current_to_mean_ratio = current_value / mean_value
        else:
            max_to_mean_ratio = 0.0
            current_to_mean_ratio = 0.0

        features = MetricsWindowFeatures(
            current_value=current_value,
            mean_value=mean_value,
            std_value=float(np.std(values, ddof=0)),
            min_value=float(np.min(values)),
            max_value=max_value,
            median_value=float(np.median(values)),
            first_value=float(values[0]),
            last_value=current_value,
            max_to_mean_ratio=float(max_to_mean_ratio),
            current_to_mean_ratio=float(current_to_mean_ratio),
            slope=float((current_value - float(values[0])) / max(sample_count - 1, 1)),
            sample_count=sample_count,
        )
        feature_vector = features.to_list()
        if (
            len(feature_vector) != 12
            or len(features.feature_names()) != 12
            or features.sample_count <= 0
            or not all(math.isfinite(value) for value in feature_vector)
        ):
            raise ValueError("MetricsWindowFeatures must contain 12 finite values")
        return features


class MetricsIForestTrainer:
    """Train the SPEC-003 Isolation Forest only from its fixed baseline fixture."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.window_builder = MetricWindowBuilder()
        self.feature_extractor = MetricsFeatureExtractor()

    def train_from_fixture(self, fixture_path: str | Path) -> dict[str, Any]:
        path = Path(fixture_path)
        if not path.exists():
            raise FileNotFoundError(str(path))
        try:
            fixture = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise MetricsIForestTrainingDataError("baseline fixture is invalid JSON") from exc
        if not isinstance(fixture, dict):
            raise MetricsIForestTrainingDataError("baseline fixture must be an object")

        metric_name = self.config["metric"]["name"]
        if fixture.get("metric_name") != metric_name:
            raise MetricsIForestTrainingDataError("baseline fixture metric_name is invalid")
        step_seconds = fixture.get("step_seconds")
        if not isinstance(step_seconds, int) or isinstance(step_seconds, bool) or step_seconds <= 0:
            raise MetricsIForestTrainingDataError("baseline fixture step_seconds is invalid")
        if step_seconds != self.config["window"]["step_seconds"]:
            raise MetricsIForestTrainingDataError("baseline fixture step_seconds does not match config")
        windows = fixture.get("windows")
        if not isinstance(windows, list):
            raise MetricsIForestTrainingDataError("baseline fixture windows must be a list")

        expected_sample_count = (
            self.config["window"]["lookback_seconds"] // self.config["window"]["step_seconds"] + 1
        )
        start_timestamps: set[float] = set()
        feature_rows: list[list[float]] = []
        for item in windows:
            samples = self._samples_from_fixture_window(
                item, step_seconds, expected_sample_count, start_timestamps
            )
            window = self.window_builder.build(
                metric_name, samples, self.config["window"]["min_sample_count"]
            )
            if window is None:  # Defensive; fixture has already been validated exactly.
                raise MetricsIForestTrainingDataError("baseline window has insufficient samples")
            feature_rows.append(self.feature_extractor.extract(window).to_list())

        minimum_window_count = self.config["training"]["minimum_window_count"]
        if len(feature_rows) < minimum_window_count:
            raise MetricsIForestTrainingDataError(
                "baseline fixture does not meet minimum_window_count"
            )
        feature_matrix = np.asarray(feature_rows, dtype=float)
        if feature_matrix.shape != (len(feature_rows), len(MetricsWindowFeatures.feature_names())):
            raise MetricsIForestTrainingDataError("training feature matrix does not match contract")
        if not np.all(np.isfinite(feature_matrix)):
            raise MetricsIForestTrainingDataError("training feature matrix contains non-finite values")

        model_params = dict(self.config["isolation_forest"])
        model = IsolationForest(**model_params)
        model.fit(feature_matrix)
        return {
            "metadata_version": self.config["model"]["metadata_version"],
            "metric_name": metric_name,
            "feature_names": MetricsWindowFeatures.feature_names(),
            "trained_at": _utc_iso_now(),
            "training_window_count": len(feature_rows),
            "model_params": model_params,
            "sklearn_version": sklearn.__version__,
            "numpy_version": np.__version__,
            "model": model,
        }

    @staticmethod
    def _samples_from_fixture_window(
        item: Any,
        step_seconds: int,
        expected_sample_count: int,
        start_timestamps: set[float],
    ) -> list[MetricSample]:
        if not isinstance(item, dict):
            raise MetricsIForestTrainingDataError("baseline window must be an object")
        try:
            start_timestamp = float(item["start_timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MetricsIForestTrainingDataError("baseline window start_timestamp is invalid") from exc
        if not math.isfinite(start_timestamp) or start_timestamp in start_timestamps:
            raise MetricsIForestTrainingDataError("baseline window start_timestamp must be unique and finite")
        values = item.get("values")
        if not isinstance(values, list) or len(values) != expected_sample_count:
            raise MetricsIForestTrainingDataError("baseline window values have an invalid count")

        samples: list[MetricSample] = []
        for index, raw_value in enumerate(values):
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise MetricsIForestTrainingDataError("baseline window contains a non-numeric value") from exc
            if not math.isfinite(value) or value < 0:
                raise MetricsIForestTrainingDataError("baseline window contains an invalid QPS value")
            samples.append(MetricSample(start_timestamp + index * step_seconds, value))
        start_timestamps.add(start_timestamp)
        return samples

    @staticmethod
    def save_artifact(artifact: dict[str, Any], model_path: str | Path) -> None:
        path = Path(model_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(artifact, path)


class MetricsIForestModelLoader:
    """Load a trusted local artifact and enforce its model contract."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def load(self, model_path: str | Path) -> dict[str, Any]:
        path = Path(model_path)
        if not path.exists():
            raise MetricsIForestModelNotFoundError(
                f"Metrics Isolation Forest model not found at {path}; run train_metrics_model.py first"
            )
        try:
            artifact = joblib.load(path)
        except Exception as exc:  # joblib may raise several deserialization exceptions.
            raise MetricsIForestModelLoadError(f"unable to load model artifact: {path}") from exc
        self._validate_artifact(artifact)
        return artifact

    def _validate_artifact(self, artifact: Any) -> None:
        if not isinstance(artifact, dict):
            raise MetricsIForestModelLoadError("model artifact must be a dictionary")

        required_keys = {
            "metadata_version",
            "metric_name",
            "feature_names",
            "trained_at",
            "training_window_count",
            "model_params",
            "sklearn_version",
            "numpy_version",
            "model",
        }
        if not required_keys.issubset(artifact):
            raise MetricsIForestModelLoadError("model artifact is missing required fields")
        if artifact["metadata_version"] != self.config["model"]["metadata_version"]:
            raise MetricsIForestModelVersionError("model metadata_version does not match config")
        if artifact["metric_name"] != self.config["metric"]["name"]:
            raise MetricsIForestModelVersionError("model metric_name does not match config")
        if artifact["feature_names"] != MetricsWindowFeatures.feature_names():
            raise MetricsIForestModelVersionError("model feature_names do not match the feature contract")
        window_count = artifact["training_window_count"]
        if (
            not isinstance(window_count, int)
            or isinstance(window_count, bool)
            or window_count < self.config["training"]["minimum_window_count"]
        ):
            raise MetricsIForestModelVersionError("model training_window_count is invalid")
        if artifact["sklearn_version"] != sklearn.__version__:
            raise MetricsIForestModelVersionError("model sklearn_version does not match runtime")
        if artifact["numpy_version"] != np.__version__:
            logger.warning("model numpy_version differs from runtime")

        model = artifact["model"]
        if not callable(getattr(model, "predict", None)) or not callable(
            getattr(model, "decision_function", None)
        ):
            raise MetricsIForestModelLoadError("model does not provide predict and decision_function")


class MetricsIForestPredictor:
    """Apply a loaded artifact to the shared 12-feature vector contract."""

    def __init__(self, artifact: dict[str, Any], anomaly_config: dict[str, Any]):
        self.model = artifact["model"]
        self.anomaly_config = anomaly_config

    def predict(self, features: MetricsWindowFeatures) -> MetricsPredictionResult:
        vector = features.to_list()
        if len(vector) != len(MetricsWindowFeatures.feature_names()) or not all(
            math.isfinite(value) for value in vector
        ):
            raise ValueError("prediction features do not meet the feature contract")
        try:
            model_label = int(self.model.predict([vector])[0])
            anomaly_score = float(self.model.decision_function([vector])[0])
        except Exception as exc:
            raise MetricsIForestModelLoadError("model prediction failed") from exc
        if not math.isfinite(anomaly_score):
            raise MetricsIForestModelLoadError("model returned a non-finite anomaly score")

        is_anomaly = (
            model_label == -1 and anomaly_score <= self.anomaly_config["score_threshold"]
        )
        return MetricsPredictionResult(
            is_anomaly=is_anomaly,
            model_label=model_label,
            anomaly_score=anomaly_score,
            confidence=self.score_to_confidence(anomaly_score),
            features=features,
        )

    def score_to_confidence(self, score: float) -> float:
        if not math.isfinite(score):
            raise ValueError("anomaly score must be finite")
        medium_score = float(self.anomaly_config["confidence_medium_score"])
        high_score = float(self.anomaly_config["confidence_high_score"])
        if score >= 0.0:
            return 0.0
        if score >= medium_score:
            return round(0.3 * (score / medium_score), 4)
        if score >= high_score:
            return round(
                0.3 + 0.5 * ((score - medium_score) / (high_score - medium_score)), 4
            )
        clamped = max(score, -1.0)
        return round(
            min(0.8 + 0.2 * ((clamped - high_score) / (-1.0 - high_score)), 1.0), 4
        )


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MetricsIForestClassifier:
    """Classify only a QPS window already judged anomalous by the model."""

    def __init__(self, classification_config: dict[str, Any]):
        self.request_spike_ratio = float(classification_config["request_spike_ratio"])
        self.known_event_type = classification_config["known_event_type"]
        self.fallback_event_type = classification_config["fallback_event_type"]

    def classify(
        self,
        window: MetricWindow,
        prediction: MetricsPredictionResult,
    ) -> MetricsClassificationResult | None:
        if not prediction.is_anomaly:
            return None
        baseline_samples = window.samples[:-1]
        if not baseline_samples:
            raise ValueError("QPS window requires at least one baseline sample")

        baseline_mean = float(fmean(sample.value for sample in baseline_samples))
        current_value = float(window.samples[-1].value)
        if baseline_mean > 0:
            spike_ratio: float | None = current_value / baseline_mean
            comparison_ratio = spike_ratio
            baseline_zero = False
        elif current_value == 0:
            spike_ratio = 0.0
            comparison_ratio = 0.0
            baseline_zero = True
        else:
            spike_ratio = None
            comparison_ratio = float("inf")
            baseline_zero = True

        if comparison_ratio >= self.request_spike_ratio:
            return MetricsClassificationResult(
                event_type=self.known_event_type,
                severity="HIGH",
                classification_reason="current_qps_at_least_3x_recent_baseline",
                baseline_mean=baseline_mean,
                spike_ratio=spike_ratio,
                baseline_zero=baseline_zero,
            )
        return MetricsClassificationResult(
            event_type=self.fallback_event_type,
            severity="HIGH" if prediction.confidence >= 0.8 else "MEDIUM",
            classification_reason="anomalous_qps_window_not_matching_known_request_spike",
            baseline_mean=baseline_mean,
            spike_ratio=spike_ratio,
            baseline_zero=baseline_zero,
        )


class MetricsIForestEventBuilder:
    """Build and JSON-validate the exact PRD-002 Metrics IForest event schema."""

    def __init__(
        self,
        metadata_version: str,
        clock: Callable[[], datetime] | None = None,
        random_suffix_provider: Callable[[], str] | None = None,
    ):
        self.metadata_version = metadata_version
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.random_suffix_provider = random_suffix_provider or (lambda: uuid.uuid4().hex[:4])

    def build(
        self,
        window: MetricWindow,
        prediction: MetricsPredictionResult,
        classification: MetricsClassificationResult,
    ) -> dict[str, Any]:
        features = prediction.features
        event = {
            "event_id": self._event_id(),
            "detected_at": _format_iso(self.clock()),
            "event_source": "metrics_iforest_detection",
            "event_type": classification.event_type,
            "detection_method": "isolation_forest",
            "severity": classification.severity,
            "confidence": float(prediction.confidence),
            "service_name": "metrics",
            "trace_id": None,
            "source_ip": None,
            "downstream_service": None,
            "external_service": None,
            "status": "OPEN",
            "triggered_features": {
                "metric_name": window.metric_name,
                "window_start": _timestamp_to_iso(window.start_timestamp),
                "window_end": _timestamp_to_iso(window.end_timestamp),
                "current_value": float(features.current_value),
                "baseline_mean": float(classification.baseline_mean),
                "spike_ratio": (
                    float(classification.spike_ratio)
                    if classification.spike_ratio is not None
                    else None
                ),
                "baseline_zero": classification.baseline_zero,
                "window_mean": float(features.mean_value),
                "window_std": float(features.std_value),
                "window_min": float(features.min_value),
                "window_max": float(features.max_value),
                "window_median": float(features.median_value),
                "window_first": float(features.first_value),
                "window_last": float(features.last_value),
                "max_to_mean_ratio": float(features.max_to_mean_ratio),
                "current_to_mean_ratio": float(features.current_to_mean_ratio),
                "slope": float(features.slope),
                "sample_count": int(features.sample_count),
                "model_label": int(prediction.model_label),
                "anomaly_score": float(prediction.anomaly_score),
                "model_metadata_version": self.metadata_version,
                "classification_reason": classification.classification_reason,
            },
            "raw_log_sample": [],
        }
        if set(event) != EVENT_FIELDS:
            raise ValueError("metrics iforest event schema does not match PRD-002")
        if event["event_type"] not in {KNOWN_EVENT_TYPE, FALLBACK_EVENT_TYPE}:
            raise ValueError("metrics iforest event_type is invalid")
        json.dumps(event, ensure_ascii=False, allow_nan=False)
        return event

    def _event_id(self) -> str:
        timestamp = int(self.clock().timestamp() * 1000)
        return f"EVT-{timestamp}-{self.random_suffix_provider()}"


class MetricsIForestCooldownManager:
    """Maintain independently expiring cooldowns for event type and metric pairs."""

    def __init__(self, cooldown_seconds: float):
        self.cooldown_seconds = cooldown_seconds
        self._last_fired: dict[tuple[str, str], float] = {}

    def should_fire(self, event_type: str, metric_name: str, now_timestamp: float) -> bool:
        last_fired = self._last_fired.get((event_type, metric_name))
        return last_fired is None or now_timestamp - last_fired >= self.cooldown_seconds

    def record_fired(self, event_type: str, metric_name: str, fired_at: float) -> None:
        self._last_fired[(event_type, metric_name)] = fired_at


class MetricsIForestDetector:
    """Coordinate the complete SPEC-003 runtime without a Rule-only fallback."""

    def __init__(
        self,
        config_path: str = "configs/metrics_iforest.yaml",
        event_store: EventStore | None = None,
    ):
        self.config = MetricsIForestConfigLoader.load(config_path)
        prometheus = self.config["prometheus"]
        self.client = PrometheusRangeClient(
            prometheus["base_url"], prometheus["query_endpoint"], prometheus["timeout_seconds"]
        )
        self.event_store = event_store or EventStore(self.config["output"]["event_store_path"])

        loader = MetricsIForestModelLoader(self.config)
        try:
            artifact = loader.load(self.config["model"]["path"])
        except MetricsIForestModelNotFoundError:
            if not self.config["model"]["train_if_missing"]:
                raise
            logger.warning("model missing; explicitly training from the fixed baseline fixture")
            trainer = MetricsIForestTrainer(self.config)
            artifact = trainer.train_from_fixture(self.config["training"]["baseline_fixture_path"])
            trainer.save_artifact(artifact, self.config["model"]["path"])
            artifact = loader.load(self.config["model"]["path"])

        self.window_builder = MetricWindowBuilder()
        self.feature_extractor = MetricsFeatureExtractor()
        self.predictor = MetricsIForestPredictor(artifact, self.config["anomaly"])
        self.classifier = MetricsIForestClassifier(self.config["classification"])
        self.event_builder = MetricsIForestEventBuilder(self.config["model"]["metadata_version"])
        self.cooldown = MetricsIForestCooldownManager(self.config["event"]["cooldown_seconds"])
        self.time_provider = time.time

    def run_once(self) -> list[dict[str, Any]]:
        now_timestamp = self.time_provider()
        window_config = self.config["window"]
        metric_name = self.config["metric"]["name"]
        samples = self.client.fetch_samples(
            metric_name,
            now_timestamp - window_config["lookback_seconds"],
            now_timestamp,
            window_config["step_seconds"],
        )
        if not samples:
            return []
        window = self.window_builder.build(
            metric_name, samples, window_config["min_sample_count"]
        )
        if window is None:
            logger.warning("insufficient QPS samples for Metrics IForest detection")
            return []
        prediction = self.predictor.predict(self.feature_extractor.extract(window))
        if not prediction.is_anomaly:
            return []
        classification = self.classifier.classify(window, prediction)
        if classification is None:
            return []
        if not self.cooldown.should_fire(classification.event_type, metric_name, now_timestamp):
            logger.debug("Metrics IForest event skipped by cooldown")
            return []

        event = self.event_builder.build(window, prediction, classification)
        try:
            self.event_store.write(event)
        except (OSError, TypeError, ValueError):
            logger.exception("failed to write Metrics IForest event")
            return []
        self.cooldown.record_fired(classification.event_type, metric_name, now_timestamp)
        return [event]

    def start(self) -> None:
        logger.info("Metrics Isolation Forest detector started")
        interval = self.config["runtime"]["poll_interval_seconds"]
        try:
            while True:
                try:
                    self.run_once()
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logger.exception("unexpected Metrics IForest runtime error")
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Metrics Isolation Forest detector stopped")


def _format_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp_to_iso(timestamp: float) -> str:
    return _format_iso(datetime.fromtimestamp(timestamp, timezone.utc))
