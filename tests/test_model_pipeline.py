import joblib
import pytest
from sklearn.base import BaseEstimator
from sklearn.ensemble import IsolationForest

import src.event_detection.model.predictor as predictor_module
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


def _dump_artifact(tmp_path, artifact):
    model_path = tmp_path / "artifact.pkl"
    joblib.dump(artifact, model_path)
    return AnomalyPredictor(_config(model_path))


class _MissingPredictEstimator(BaseEstimator):
    fitted_ = True
    n_features_in_ = 23

    def fit(self, _features):
        return self

    def decision_function(self, _features):
        return [0.0]


class _MissingDecisionFunctionEstimator(BaseEstimator):
    fitted_ = True
    n_features_in_ = 23

    def fit(self, _features):
        return self

    def predict(self, _features):
        return [1]


class _NonCallablePredictEstimator(_MissingPredictEstimator):
    predict = "not-callable"


class _NonCallableDecisionFunctionEstimator(_MissingDecisionFunctionEstimator):
    decision_function = "not-callable"


class _UnfittedInterfaceEstimator(BaseEstimator):
    def fit(self, _features):
        return self

    def predict(self, _features):
        return [1]

    def decision_function(self, _features):
        return [0.0]


class _FittedWithoutDimensionEstimator(_UnfittedInterfaceEstimator):
    def __init__(self):
        self.fitted_ = True


class _FittedInvalidDimensionEstimator(_FittedWithoutDimensionEstimator):
    def __init__(self):
        super().__init__()
        self.n_features_in_ = "invalid"


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


def test_window_feature_vector_public_contract_has_23_features():
    assert len(WindowFeatureVector.feature_names()) == 23
    assert len(WindowFeatureVector().to_list()) == 23


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        (None, "artifact is None"),
        ({"model": object()}, "unsupported model artifact type: dict"),
        ("not-a-model", "unsupported model artifact type: str"),
        (object(), "does not provide callable predict"),
    ],
)
def test_predictor_load_rejects_invalid_artifact_types(
    tmp_path, artifact, message
):
    predictor = _dump_artifact(tmp_path, artifact)

    with pytest.raises(TypeError, match=message):
        predictor.load()

    assert predictor._model is None


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        (_MissingPredictEstimator(), "callable predict"),
        (_MissingDecisionFunctionEstimator(), "callable decision_function"),
        (_NonCallablePredictEstimator(), "callable predict"),
        (_NonCallableDecisionFunctionEstimator(), "callable decision_function"),
    ],
)
def test_predictor_load_requires_callable_inference_interface(
    tmp_path, artifact, message
):
    predictor = _dump_artifact(tmp_path, artifact)

    with pytest.raises(TypeError, match=message):
        predictor.load()

    assert predictor._model is None


def test_predictor_load_rejects_unfitted_isolation_forest(tmp_path):
    predictor = _dump_artifact(tmp_path, IsolationForest(random_state=42))

    with pytest.raises(ValueError, match="model artifact is not fitted") as error:
        predictor.load()

    assert error.value.__cause__ is not None
    assert predictor._model is None


def test_predictor_load_rejects_fake_estimator_without_fitted_state(tmp_path):
    predictor = _dump_artifact(tmp_path, _UnfittedInterfaceEstimator())

    with pytest.raises(ValueError, match="model artifact is not fitted"):
        predictor.load()

    assert predictor._model is None


def test_predictor_load_rejects_wrong_feature_dimension(tmp_path):
    model = IsolationForest(random_state=42).fit([[float(index)] * 10 for index in range(50)])
    predictor = _dump_artifact(tmp_path, model)

    with pytest.raises(
        ValueError, match="feature dimension mismatch: expected 23, got 10"
    ):
        predictor.load()

    assert predictor._model is None


def test_predictor_load_requires_n_features_in(tmp_path):
    predictor = _dump_artifact(tmp_path, _FittedWithoutDimensionEstimator())

    with pytest.raises(ValueError, match="missing n_features_in_"):
        predictor.load()

    assert predictor._model is None


def test_predictor_load_rejects_invalid_n_features_in_type(tmp_path):
    predictor = _dump_artifact(tmp_path, _FittedInvalidDimensionEstimator())

    with pytest.raises(TypeError, match="n_features_in_ must be an integer") as error:
        predictor.load()

    assert error.value.__cause__ is not None
    assert predictor._model is None


def test_predictor_load_failure_preserves_empty_state(tmp_path, monkeypatch):
    model_path = tmp_path / "artifact.pkl"
    model_path.write_bytes(b"placeholder")
    predictor = AnomalyPredictor(_config(model_path))

    def failing_load(_path):
        raise OSError("artifact read failed")

    monkeypatch.setattr(predictor_module.joblib, "load", failing_load)

    with pytest.raises(OSError, match="artifact read failed"):
        predictor.load()

    assert predictor._model is None


def test_predictor_can_retry_with_valid_model_after_validation_failure(tmp_path):
    model_path = tmp_path / "artifact.pkl"
    config = _config(model_path)
    joblib.dump({"model": object()}, model_path)
    predictor = AnomalyPredictor(config)

    with pytest.raises(TypeError, match="unsupported model artifact type"):
        predictor.load()
    assert predictor._model is None

    ModelTrainer(config).train(_normal_windows())
    predictor.load()

    assert predictor._model is not None
    assert predictor._model.n_features_in_ == len(WindowFeatureVector.feature_names())


def test_confidence_mapping_is_bounded_and_increases_with_anomaly(tmp_path):
    predictor = AnomalyPredictor(_config(tmp_path / "model.pkl"))
    confidences = [predictor._score_to_confidence(score)
                   for score in (0.1, -0.05, -0.2, -0.5, -2.0)]
    assert confidences == sorted(confidences)
    assert confidences[0] == 0.0
    assert confidences[-1] == 1.0
