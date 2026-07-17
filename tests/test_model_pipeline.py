import pytest

from src.event_detection.model.predictor import AnomalyPredictor, PredictionResult
from src.event_detection.model.schema import EncodedFeatureVector, WindowFeatureVector
from src.event_detection.model.trainer import ModelTrainer


def _config(path):
    return {
        "isolation_forest": {"contamination": 0.05, "n_estimators": 50,
                             "random_state": 42, "max_samples": "auto"},
        "anomaly": {"score_threshold": 0.1, "confidence_high_threshold": -0.3,
                    "confidence_medium_threshold": -0.1},
        "output": {"model_path": str(path)},
    }


def _normal_windows(count=50):
    return [WindowFeatureVector(total_log_count=10 + index % 3,
                                mean_duration_ms=20 + index % 5,
                                max_duration_ms=30 + index % 5)
            for index in range(count)]


def test_trainer_requires_at_least_50_windows(tmp_path):
    with pytest.raises(ValueError):
        ModelTrainer(_config(tmp_path / "model.pkl")).train(_normal_windows(49))


def test_trainer_rejects_wrong_vector_type(tmp_path):
    with pytest.raises(TypeError):
        ModelTrainer(_config(tmp_path / "model.pkl")).train([EncodedFeatureVector()] * 50)


def test_model_save_load_and_prediction(tmp_path):
    model_path = tmp_path / "nested" / "model.pkl"
    config = _config(model_path)
    model = ModelTrainer(config).train(_normal_windows())
    assert model is not None
    assert model_path.exists()

    predictor = AnomalyPredictor(config)
    with pytest.raises(RuntimeError):
        predictor.predict_one(WindowFeatureVector())
    predictor.load()
    result = predictor.predict_one(WindowFeatureVector(
        total_log_count=500, error_count=500, error_rate=1,
        status_401_count=500, max_same_source_ip_count=500,
    ))
    assert isinstance(result, PredictionResult)
    assert result.label in (-1, 1)
    assert 0.0 <= result.confidence <= 1.0
    assert isinstance(result.is_anomaly, bool)

    batch = predictor.predict_batch(_normal_windows(2))
    assert len(batch) == 2
    assert all(isinstance(item, PredictionResult) for item in batch)


def test_confidence_mapping_is_bounded_and_increases_with_anomaly(tmp_path):
    predictor = AnomalyPredictor(_config(tmp_path / "model.pkl"))
    confidences = [predictor._score_to_confidence(score)
                   for score in (0.1, -0.05, -0.2, -0.5, -2.0)]
    assert confidences == sorted(confidences)
    assert confidences[0] == 0.0
    assert confidences[-1] == 1.0
