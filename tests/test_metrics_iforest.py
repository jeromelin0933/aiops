import json
import math
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pytest
import yaml

from src.event_detection.metrics_iforest import (
    MetricSample,
    MetricWindow,
    MetricWindowBuilder,
    MetricsClassificationResult,
    MetricsIForestClassifier,
    MetricsFeatureExtractor,
    MetricsIForestConfigLoader,
    MetricsIForestModelLoadError,
    MetricsIForestModelLoader,
    MetricsIForestModelNotFoundError,
    MetricsIForestModelVersionError,
    MetricsIForestPredictor,
    MetricsIForestTrainer,
    MetricsIForestTrainingDataError,
    MetricsIForestCooldownManager,
    MetricsIForestDetector,
    MetricsIForestEventBuilder,
    MetricsPredictionResult,
    MetricsWindowFeatures,
    PrometheusRangeClient,
)


FIXTURES = Path(__file__).parent / "fixtures" / "metrics_iforest"
CONFIG_PATH = Path("configs/metrics_iforest.yaml")


class FakeResponse:
    def __init__(self, payload=None, json_error=None, status_error=None):
        self.payload = payload
        self.json_error = json_error
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def write_config(tmp_path, config):
    path = tmp_path / "metrics_iforest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def test_config_loader_loads_spec_config():
    config = MetricsIForestConfigLoader.load(CONFIG_PATH)

    assert config["prometheus"]["query_endpoint"] == "/api/v1/query_range"
    assert config["metric"]["name"] == "api_requests_per_sec"
    assert config["window"]["lookback_seconds"] / config["window"]["step_seconds"] + 1 == 21


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("prometheus", "query_endpoint", "/api/v1/query"),
        ("metric", "name", "system_memory_usage_pct"),
        ("window", "min_sample_count", 1),
        ("window", "lookback_seconds", 301),
        ("isolation_forest", "contamination", 0.5),
        ("classification", "request_spike_ratio", 1.0),
        ("model", "train_if_missing", "false"),
        ("training", "minimum_window_count", 9),
    ],
)
def test_config_loader_rejects_invalid_contract(tmp_path, section, key, value):
    config = MetricsIForestConfigLoader.load(CONFIG_PATH)
    config[section][key] = value

    with pytest.raises(ValueError):
        MetricsIForestConfigLoader.load(write_config(tmp_path, config))


def test_config_loader_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        MetricsIForestConfigLoader.load(tmp_path / "missing.yaml")


def test_range_client_uses_query_range_and_parses_one_matrix_series(monkeypatch):
    captured = {}

    def fake_get(url, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        return FakeResponse(load_fixture("prometheus_qps_normal.json"))

    monkeypatch.setattr("src.event_detection.metrics_iforest.requests.get", fake_get)
    samples = PrometheusRangeClient(
        "http://prometheus.test/", "/api/v1/query_range", 5
    ).fetch_samples("api_requests_per_sec", 1720000000.0, 1720000030.0, 15)

    assert captured == {
        "url": "http://prometheus.test/api/v1/query_range",
        "params": {
            "query": "api_requests_per_sec",
            "start": 1720000000.0,
            "end": 1720000030.0,
            "step": 15,
        },
        "timeout": 5,
    }
    assert len(samples) == 21
    assert samples[0] == MetricSample(1720000000.0, 10.0)
    assert samples[-1] == MetricSample(1720000300.0, 10.1)


def test_range_client_filters_invalid_samples_and_keeps_last_duplicate(monkeypatch):
    monkeypatch.setattr(
        "src.event_detection.metrics_iforest.requests.get",
        lambda *_args, **_kwargs: FakeResponse(load_fixture("prometheus_qps_malformed.json")),
    )

    samples = PrometheusRangeClient("http://prometheus.test", "/api/v1/query_range", 5).fetch_samples(
        "api_requests_per_sec", 1, 2, 15
    )

    assert samples == [
        MetricSample(1720000000.0, 8.0),
        MetricSample(1720000030.0, 12.0),
        MetricSample(1720000090.0, 11.0),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        load_fixture("prometheus_qps_empty.json"),
        {"status": "error", "data": {}},
        {"status": "success", "data": {"resultType": "vector", "result": []}},
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"values": []}, {"values": []}],
            },
        },
    ],
)
def test_range_client_returns_empty_for_non_single_usable_matrix(monkeypatch, payload):
    monkeypatch.setattr(
        "src.event_detection.metrics_iforest.requests.get",
        lambda *_args, **_kwargs: FakeResponse(payload),
    )

    assert PrometheusRangeClient("http://prometheus.test", "/api/v1/query_range", 5).fetch_samples(
        "api_requests_per_sec", 1, 2, 15
    ) == []


def test_range_client_returns_empty_for_request_or_json_error(monkeypatch):
    import requests

    monkeypatch.setattr(
        "src.event_detection.metrics_iforest.requests.get",
        lambda *_args, **_kwargs: FakeResponse(json_error=ValueError("invalid json")),
    )
    client = PrometheusRangeClient("http://prometheus.test", "/api/v1/query_range", 5)
    assert client.fetch_samples("api_requests_per_sec", 1, 2, 15) == []

    monkeypatch.setattr(
        "src.event_detection.metrics_iforest.requests.get",
        lambda *_args, **_kwargs: FakeResponse(status_error=requests.HTTPError("bad status")),
    )
    assert client.fetch_samples("api_requests_per_sec", 1, 2, 15) == []


def test_window_builder_uses_actual_sample_bounds_and_rejects_insufficient_samples():
    samples = [MetricSample(30, 3), MetricSample(10, 1), MetricSample(20, 2)]
    builder = MetricWindowBuilder()

    assert builder.build("api_requests_per_sec", samples, 4) is None
    window = builder.build("api_requests_per_sec", samples, 3)

    assert window.start_timestamp == 10
    assert window.end_timestamp == 30
    assert [sample.value for sample in window.samples] == [1, 2, 3]
    with pytest.raises(ValueError):
        builder.build("db_pool_active_connections", samples, 3)


def test_feature_extractor_matches_12_feature_contract_and_population_std():
    window = MetricWindow(
        "api_requests_per_sec",
        10,
        40,
        [MetricSample(10, 2.0), MetricSample(20, 4.0), MetricSample(30, 6.0), MetricSample(40, 8.0)],
    )

    features = MetricsFeatureExtractor().extract(window)

    assert features.feature_names() == [
        "current_value", "mean_value", "std_value", "min_value", "max_value", "median_value",
        "first_value", "last_value", "max_to_mean_ratio", "current_to_mean_ratio", "slope", "sample_count",
    ]
    assert features.to_list() == pytest.approx(
        [8.0, 5.0, 2.2360679775, 2.0, 8.0, 5.0, 2.0, 8.0, 1.6, 1.6, 2.0, 4.0]
    )
    assert len(features.feature_names()) == len(features.to_list()) == 12


def test_feature_extractor_prevents_non_finite_ratios_when_mean_is_zero():
    window = MetricWindow(
        "api_requests_per_sec", 10, 25,
        [MetricSample(10, 0.0), MetricSample(25, 0.0)],
    )

    features = MetricsFeatureExtractor().extract(window)

    assert features.max_to_mean_ratio == 0.0
    assert features.current_to_mean_ratio == 0.0
    assert all(math.isfinite(value) for value in features.to_list())


def test_feature_extractor_rejects_non_finite_window_values():
    window = MetricWindow(
        "api_requests_per_sec", 1, 2,
        [MetricSample(1, 1.0), MetricSample(2, float("nan"))],
    )

    with pytest.raises(ValueError):
        MetricsFeatureExtractor().extract(window)


def phase_three_config(tmp_path):
    config = MetricsIForestConfigLoader.load(CONFIG_PATH)
    config["model"]["path"] = str(tmp_path / "metrics_isolation_forest.pkl")
    config["training"]["baseline_fixture_path"] = str(FIXTURES / "qps_baseline.json")
    return config


def test_trainer_builds_reproducible_artifact_and_loader_validates_it(tmp_path):
    config = phase_three_config(tmp_path)
    trainer = MetricsIForestTrainer(config)

    artifact = trainer.train_from_fixture(config["training"]["baseline_fixture_path"])
    trainer.save_artifact(artifact, config["model"]["path"])
    loaded = MetricsIForestModelLoader(config).load(config["model"]["path"])

    assert artifact["training_window_count"] == 30
    assert artifact["feature_names"] == MetricsWindowFeatures.feature_names()
    assert artifact["metric_name"] == "api_requests_per_sec"
    assert artifact["metadata_version"] == "1.0"
    assert artifact["model_params"] == config["isolation_forest"]
    assert loaded["sklearn_version"] == artifact["sklearn_version"]
    assert callable(loaded["model"].predict)
    assert callable(loaded["model"].decision_function)


def test_trainer_rejects_baseline_with_insufficient_windows(tmp_path):
    config = phase_three_config(tmp_path)
    fixture = {
        "metric_name": "api_requests_per_sec",
        "step_seconds": 15,
        "windows": [{"start_timestamp": 1, "values": [10.0] * 21}],
    }
    fixture_path = tmp_path / "small_baseline.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(MetricsIForestTrainingDataError):
        MetricsIForestTrainer(config).train_from_fixture(fixture_path)


@pytest.mark.parametrize(
    "mutate,expected_exception",
    [
        (lambda artifact: artifact.update(metadata_version="2.0"), MetricsIForestModelVersionError),
        (lambda artifact: artifact.update(metric_name="db_pool_active_connections"), MetricsIForestModelVersionError),
        (lambda artifact: artifact.update(feature_names=["wrong"]), MetricsIForestModelVersionError),
        (lambda artifact: artifact.update(training_window_count=1), MetricsIForestModelVersionError),
        (lambda artifact: artifact.update(sklearn_version="0.0"), MetricsIForestModelVersionError),
        (lambda artifact: artifact.pop("model"), MetricsIForestModelLoadError),
    ],
)
def test_loader_rejects_invalid_artifact_metadata(tmp_path, mutate, expected_exception):
    config = phase_three_config(tmp_path)
    artifact = MetricsIForestTrainer(config).train_from_fixture(
        config["training"]["baseline_fixture_path"]
    )
    mutate(artifact)
    joblib.dump(artifact, config["model"]["path"])

    with pytest.raises(expected_exception):
        MetricsIForestModelLoader(config).load(config["model"]["path"])


def test_loader_fails_fast_when_model_is_missing(tmp_path):
    config = phase_three_config(tmp_path)

    with pytest.raises(MetricsIForestModelNotFoundError, match="train_metrics_model.py"):
        MetricsIForestModelLoader(config).load(config["model"]["path"])


class StubModel:
    def __init__(self, label, score):
        self.label = label
        self.score = score
        self.vectors = []

    def predict(self, vectors):
        self.vectors.extend(vectors)
        return [self.label]

    def decision_function(self, _vectors):
        return [self.score]


def extracted_features():
    return MetricsFeatureExtractor().extract(
        MetricWindow(
            "api_requests_per_sec", 1, 3,
            [MetricSample(1, 10.0), MetricSample(2, 10.5), MetricSample(3, 11.0)],
        )
    )


@pytest.mark.parametrize(
    "label,score,expected",
    [(-1, -0.06, True), (-1, -0.04, False), (1, -0.40, False)],
)
def test_predictor_requires_label_and_score_gate(label, score, expected):
    config = MetricsIForestConfigLoader.load(CONFIG_PATH)
    model = StubModel(label, score)
    features = extracted_features()

    result = MetricsIForestPredictor({"model": model}, config["anomaly"]).predict(features)

    assert result.is_anomaly is expected
    assert result.model_label == label
    assert result.anomaly_score == score
    assert model.vectors == [features.to_list()]


@pytest.mark.parametrize(
    "score,expected_confidence",
    [(0.1, 0.0), (-0.05, 0.15), (-0.2, 0.55), (-0.5, 0.8571), (-2.0, 1.0)],
)
def test_predictor_confidence_is_finite_and_bounded(score, expected_confidence):
    config = MetricsIForestConfigLoader.load(CONFIG_PATH)
    predictor = MetricsIForestPredictor({"model": StubModel(-1, score)}, config["anomaly"])

    confidence = predictor.score_to_confidence(score)

    assert confidence == expected_confidence
    assert math.isfinite(confidence)
    assert 0.0 <= confidence <= 1.0


def anomaly_prediction(features, confidence=0.9):
    return MetricsPredictionResult(True, -1, -0.4, confidence, features)


def test_classifier_distinguishes_known_spike_boundary_and_general_anomaly():
    config = MetricsIForestConfigLoader.load(CONFIG_PATH)
    classifier = MetricsIForestClassifier(config["classification"])
    spike_window = MetricWindow(
        "api_requests_per_sec", 1, 3,
        [MetricSample(1, 10.0), MetricSample(2, 10.0), MetricSample(3, 30.0)],
    )
    spike = classifier.classify(
        spike_window, anomaly_prediction(MetricsFeatureExtractor().extract(spike_window))
    )
    general_window = MetricWindow(
        "api_requests_per_sec", 1, 3,
        [MetricSample(1, 10.0), MetricSample(2, 10.0), MetricSample(3, 20.0)],
    )
    general = classifier.classify(
        general_window, anomaly_prediction(MetricsFeatureExtractor().extract(general_window), 0.6)
    )
    high_confidence_general = classifier.classify(
        general_window, anomaly_prediction(MetricsFeatureExtractor().extract(general_window), 0.8)
    )

    assert (spike.event_type, spike.severity, spike.spike_ratio) == (
        "request_spike_detected", "HIGH", 3.0
    )
    assert (general.event_type, general.severity) == ("general_metrics_anomaly", "MEDIUM")
    assert high_confidence_general.severity == "HIGH"


def test_classifier_never_classifies_a_normal_model_prediction_even_when_ratio_is_high():
    window = MetricWindow(
        "api_requests_per_sec", 1, 3,
        [MetricSample(1, 1.0), MetricSample(2, 1.0), MetricSample(3, 30.0)],
    )
    features = MetricsFeatureExtractor().extract(window)
    normal_prediction = MetricsPredictionResult(False, 1, 0.1, 0.0, features)

    assert MetricsIForestClassifier(
        MetricsIForestConfigLoader.load(CONFIG_PATH)["classification"]
    ).classify(window, normal_prediction) is None


def test_baseline_zero_produces_json_safe_known_spike_event():
    window = MetricWindow(
        "api_requests_per_sec", 1, 3,
        [MetricSample(1, 0.0), MetricSample(2, 0.0), MetricSample(3, 5.0)],
    )
    prediction = anomaly_prediction(MetricsFeatureExtractor().extract(window))
    classification = MetricsIForestClassifier(
        MetricsIForestConfigLoader.load(CONFIG_PATH)["classification"]
    ).classify(window, prediction)
    event = MetricsIForestEventBuilder(
        "1.0",
        clock=lambda: datetime(2026, 7, 26, 10, 5, 1, 234000, tzinfo=timezone.utc),
        random_suffix_provider=lambda: "a3f9",
    ).build(window, prediction, classification)

    assert classification.baseline_zero is True
    assert classification.spike_ratio is None
    assert event["triggered_features"]["spike_ratio"] is None
    json.dumps(event, allow_nan=False)


def test_event_builder_outputs_exact_schema_and_fixed_metrics_mapping():
    window = MetricWindow(
        "api_requests_per_sec", 1720000000, 1720000030,
        [MetricSample(1720000000, 10.0), MetricSample(1720000015, 10.0), MetricSample(1720000030, 30.0)],
    )
    prediction = anomaly_prediction(MetricsFeatureExtractor().extract(window))
    classification = MetricsIForestClassifier(
        MetricsIForestConfigLoader.load(CONFIG_PATH)["classification"]
    ).classify(window, prediction)
    event = MetricsIForestEventBuilder(
        "1.0",
        clock=lambda: datetime(2026, 7, 26, 10, 5, 1, 234000, tzinfo=timezone.utc),
        random_suffix_provider=lambda: "a3f9",
    ).build(window, prediction, classification)

    assert len(event) == 15
    assert set(event) == {
        "event_id", "detected_at", "event_source", "event_type", "detection_method",
        "severity", "confidence", "service_name", "trace_id", "source_ip",
        "downstream_service", "external_service", "status", "triggered_features", "raw_log_sample",
    }
    assert event["event_source"] == "metrics_iforest_detection"
    assert event["detection_method"] == "isolation_forest"
    assert event["service_name"] == "metrics"
    assert event["raw_log_sample"] == []
    assert "window_start" not in event
    assert event["triggered_features"]["classification_reason"] == "current_qps_at_least_3x_recent_baseline"


def test_cooldown_key_uses_event_type_and_metric_and_only_updates_when_recorded():
    cooldown = MetricsIForestCooldownManager(60)

    assert cooldown.should_fire("request_spike_detected", "api_requests_per_sec", 1000)
    cooldown.record_fired("request_spike_detected", "api_requests_per_sec", 1000)
    assert not cooldown.should_fire("request_spike_detected", "api_requests_per_sec", 1059)
    assert cooldown.should_fire("general_metrics_anomaly", "api_requests_per_sec", 1001)
    assert cooldown.should_fire("request_spike_detected", "another_metric", 1001)
    assert cooldown.should_fire("request_spike_detected", "api_requests_per_sec", 1060)


class RecordingStore:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def write(self, event):
        if self.fail:
            raise OSError("write failed")
        self.events.append(event)


class SampleClient:
    def __init__(self, samples):
        self.samples = samples

    def fetch_samples(self, *_args):
        return self.samples


class WindowPredictor:
    def __init__(self, is_anomaly, confidence=0.9):
        self.is_anomaly = is_anomaly
        self.confidence = confidence

    def predict(self, features):
        return MetricsPredictionResult(
            self.is_anomaly,
            -1 if self.is_anomaly else 1,
            -0.4 if self.is_anomaly else 0.1,
            self.confidence if self.is_anomaly else 0.0,
            features,
        )


def detector_with_samples(tmp_path, samples, store=None):
    config = phase_three_config(tmp_path)
    artifact = MetricsIForestTrainer(config).train_from_fixture(
        config["training"]["baseline_fixture_path"]
    )
    MetricsIForestTrainer.save_artifact(artifact, config["model"]["path"])
    config_path = write_config(tmp_path, config)
    detector = MetricsIForestDetector(str(config_path), event_store=store or RecordingStore())
    detector.client = SampleClient(samples)
    detector.time_provider = lambda: 1720000030.0
    return detector


def test_detector_writes_known_and_general_events_and_blocks_rule_only(tmp_path):
    spike_samples = [MetricSample(1720000000 + index * 15, 10.0) for index in range(20)] + [
        MetricSample(1720000300, 30.0)
    ]
    known_store = RecordingStore()
    known_detector = detector_with_samples(tmp_path, spike_samples, known_store)
    known_detector.predictor = WindowPredictor(True, 0.9)
    known = known_detector.run_once()

    assert [event["event_type"] for event in known] == ["request_spike_detected"]
    assert known_store.events == known

    normal_store = RecordingStore()
    normal_detector = detector_with_samples(tmp_path / "normal", spike_samples, normal_store)
    normal_detector.predictor = WindowPredictor(False)
    assert normal_detector.run_once() == []
    assert normal_store.events == []

    general_samples = [MetricSample(1720000000 + index * 15, 10.0) for index in range(20)] + [
        MetricSample(1720000300, 20.0)
    ]
    general_detector = detector_with_samples(tmp_path / "general", general_samples)
    general_detector.predictor = WindowPredictor(True, 0.6)
    assert general_detector.run_once()[0]["event_type"] == "general_metrics_anomaly"


def test_detector_write_failure_does_not_record_cooldown(tmp_path):
    samples = [MetricSample(1720000000 + index * 15, 10.0) for index in range(20)] + [
        MetricSample(1720000300, 30.0)
    ]
    detector = detector_with_samples(tmp_path, samples, RecordingStore(fail=True))
    detector.predictor = WindowPredictor(True)

    assert detector.run_once() == []
    assert detector.cooldown.should_fire("request_spike_detected", "api_requests_per_sec", 1720000031)


def test_detector_train_if_missing_uses_only_fixed_baseline_fixture(tmp_path):
    config = phase_three_config(tmp_path)
    config["model"]["train_if_missing"] = True
    config_path = write_config(tmp_path, config)

    detector = MetricsIForestDetector(str(config_path), event_store=RecordingStore())

    assert Path(config["model"]["path"]).exists()
    assert detector.predictor.model is not None


def test_detector_start_recovers_one_runtime_error_then_stops_on_keyboard_interrupt(tmp_path, monkeypatch):
    detector = detector_with_samples(tmp_path, [])
    outcomes = iter([RuntimeError("temporary failure"), KeyboardInterrupt()])
    monkeypatch.setattr(detector, "run_once", lambda: (_ for _ in ()).throw(next(outcomes)))
    monkeypatch.setattr("src.event_detection.metrics_iforest.time.sleep", lambda _seconds: None)

    detector.start()
