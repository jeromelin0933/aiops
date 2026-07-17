"""Train and persist a window-level Isolation Forest model."""

from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from src.event_detection.model.schema import WindowFeatureVector


class ModelTrainer:
    def __init__(self, config: dict):
        model_config = config["isolation_forest"]
        self.model_path = Path(config["output"]["model_path"])
        self.parameters = {
            "contamination": model_config["contamination"],
            "n_estimators": model_config["n_estimators"],
            "random_state": model_config["random_state"],
            "max_samples": model_config["max_samples"],
        }

    def train(self, vectors: list[WindowFeatureVector]) -> IsolationForest:
        if not isinstance(vectors, list) or any(
            not isinstance(vector, WindowFeatureVector) for vector in vectors
        ):
            raise TypeError("vectors must be a list of WindowFeatureVector")
        if len(vectors) < 50:
            raise ValueError("at least 50 WindowFeatureVector samples are required")

        features = np.asarray([vector.to_list() for vector in vectors], dtype=float)
        model = IsolationForest(n_jobs=-1, **self.parameters)
        model.fit(features)
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, self.model_path)
        return model
