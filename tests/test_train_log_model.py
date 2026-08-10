from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pytest

import scripts.train_log_model as train_log_model
from src.event_detection.model.predictor import AnomalyPredictor
from src.event_detection.model.schema import WindowFeatureVector


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "configs" / "event_detection.yml"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "log_model" / "normal_baseline.jsonl"


def _config(tmp_path):
    config = train_log_model.load_config(CONFIG_PATH)
    config["output"]["model_path"] = str(tmp_path / "log_isolation_forest.pkl")
    return config


def test_fixed_normal_baseline_is_parseable_and_contains_only_normal_traffic():
    entries = train_log_model.load_normal_baseline(FIXTURE_PATH)

    assert entries
    assert all(entry.level == "INFO" for entry in entries)
    assert all(entry.status_code == 200 for entry in entries)
    assert all(entry.error_type == "unknown" for entry in entries)
    assert all(not entry.is_error and not entry.is_warn for entry in entries)
    assert all(not entry.is_4xx and not entry.is_5xx and not entry.is_oom for entry in entries)


def test_fixture_constructs_only_valid_23_dimension_window_vectors(tmp_path):
    vectors = train_log_model.build_training_vectors(
        _config(tmp_path), train_log_model.load_normal_baseline(FIXTURE_PATH)
    )

    assert all(isinstance(vector, WindowFeatureVector) for vector in vectors)
    assert all(len(vector.to_list()) == 23 for vector in vectors)


def test_training_window_count_meets_formal_minimum(tmp_path):
    config = _config(tmp_path)
    vectors = train_log_model.build_training_vectors(
        config, train_log_model.load_normal_baseline(FIXTURE_PATH)
    )

    assert len(vectors) >= config["training"]["log_model"]["minimum_window_count"]


def test_training_fails_when_fixture_has_fewer_than_formal_minimum_windows(tmp_path):
    config = _config(tmp_path)
    config["training"]["log_model"]["minimum_window_count"] = 51

    with pytest.raises(ValueError, match="requires at least 51"):
        train_log_model.build_training_vectors(
            config, train_log_model.load_normal_baseline(FIXTURE_PATH)
        )


def test_training_uses_existing_formal_isolation_forest_config(tmp_path, monkeypatch):
    config = _config(tmp_path)
    captured = {}
    real_trainer = train_log_model.ModelTrainer

    class CapturingTrainer(real_trainer):
        def __init__(self, received_config):
            captured["parameters"] = {
                key: received_config["isolation_forest"][key]
                for key in ("contamination", "n_estimators", "random_state", "max_samples")
            }
            super().__init__(received_config)

    monkeypatch.setattr(train_log_model, "ModelTrainer", CapturingTrainer)
    train_log_model.train_log_model(config)

    assert captured["parameters"] == config["isolation_forest"]


def test_training_writes_reloadable_fitted_estimator_to_tmp_path(tmp_path):
    config = _config(tmp_path)
    model, vectors = train_log_model.train_log_model(config)
    artifact_path = Path(config["output"]["model_path"])

    assert artifact_path.parent == tmp_path
    assert artifact_path.exists()
    assert model.n_features_in_ == 23
    assert len(vectors) >= config["training"]["log_model"]["minimum_window_count"]

    predictor = AnomalyPredictor(config)
    predictor.load()
    assert predictor._model is not None
    assert callable(predictor._model.predict)
    assert callable(predictor._model.decision_function)
    assert predictor._model.n_features_in_ == 23


def test_fixed_random_state_produces_reproducible_predictions(tmp_path):
    first_config = _config(tmp_path / "first")
    second_config = _config(tmp_path / "second")

    train_log_model.train_log_model(first_config)
    train_log_model.train_log_model(second_config)

    first_model = joblib.load(first_config["output"]["model_path"])
    second_model = joblib.load(second_config["output"]["model_path"])
    sample = np.asarray(
        train_log_model.build_training_vectors(
            first_config, train_log_model.load_normal_baseline(FIXTURE_PATH)
        )[0].to_list(),
        dtype=float,
    ).reshape(1, -1)

    np.testing.assert_array_equal(first_model.predict(sample), second_model.predict(sample))
    np.testing.assert_allclose(
        first_model.decision_function(sample), second_model.decision_function(sample)
    )
