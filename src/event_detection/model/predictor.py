"""Load and run a window-level Isolation Forest model."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from src.event_detection.model.schema import WindowFeatureVector


@dataclass(frozen=True)
class PredictionResult:
    is_anomaly: bool
    anomaly_score: float
    confidence: float
    label: int


class AnomalyPredictor:
    def __init__(self, config: dict):
        self.model_path = Path(config["output"]["model_path"])
        anomaly = config["anomaly"]
        self.score_threshold = float(anomaly["score_threshold"])
        self.confidence_high_threshold = float(anomaly["confidence_high_threshold"])
        self.confidence_medium_threshold = float(anomaly["confidence_medium_threshold"])
        self._model: Optional[IsolationForest] = None

    def load(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"model not found: {self.model_path}")
        self._model = joblib.load(self.model_path)

    def predict_one(self, vector: WindowFeatureVector) -> PredictionResult:
        if self._model is None:
            raise RuntimeError("model must be loaded before prediction")
        if not isinstance(vector, WindowFeatureVector):
            raise TypeError("vector must be a WindowFeatureVector")
        features = np.asarray([vector.to_list()], dtype=float)
        score = float(self._model.decision_function(features)[0])
        label = int(self._model.predict(features)[0])
        return PredictionResult(
            is_anomaly=label == -1 and score < self.score_threshold,
            anomaly_score=score,
            confidence=self._score_to_confidence(score),
            label=label,
        )

    def predict_batch(self, vectors: list[WindowFeatureVector]) -> list[PredictionResult]:
        if self._model is None:
            raise RuntimeError("model must be loaded before prediction")
        if not isinstance(vectors, list) or any(
            not isinstance(vector, WindowFeatureVector) for vector in vectors
        ):
            raise TypeError("vectors must be a list of WindowFeatureVector")
        return [self.predict_one(vector) for vector in vectors]

    def _score_to_confidence(self, score: float) -> float:
        if score >= 0.0:
            return 0.0
        if score >= self.confidence_medium_threshold:
            ratio = score / self.confidence_medium_threshold
            return round(0.3 * ratio, 3)
        if score >= self.confidence_high_threshold:
            ratio = (
                (score - self.confidence_medium_threshold)
                / (self.confidence_high_threshold - self.confidence_medium_threshold)
            )
            return round(0.3 + 0.5 * ratio, 3)
        clamped = max(score, -1.0)
        ratio = (
            (clamped - self.confidence_high_threshold)
            / (-1.0 - self.confidence_high_threshold)
        )
        return round(min(0.8 + 0.2 * ratio, 1.0), 3)
