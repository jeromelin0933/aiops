"""Train the fixed-baseline Log Isolation Forest model."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.event_detection.log.features import FeatureExtractor  # noqa: E402
from src.event_detection.log.parser import LogParser  # noqa: E402
from src.event_detection.log.reader import LogReader  # noqa: E402
from src.event_detection.log.window import WindowFeatureAggregator  # noqa: E402
from src.event_detection.model.predictor import AnomalyPredictor  # noqa: E402
from src.event_detection.model.schema import RawFeatures, WindowFeatureVector  # noqa: E402
from src.event_detection.model.trainer import ModelTrainer  # noqa: E402


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load the existing log-detection configuration."""
    with Path(config_path).open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    if not isinstance(config, dict):
        raise ValueError("event detection config must be a mapping")
    return config


def _training_config(config: dict[str, Any]) -> dict[str, Any]:
    try:
        training = config["training"]["log_model"]
        window = config["window"]
    except KeyError as exc:
        raise ValueError(f"missing log model training configuration: {exc}") from exc
    if not isinstance(training, dict) or not isinstance(window, dict):
        raise ValueError("log model training and window configuration must be mappings")

    minimum_window_count = training.get("minimum_window_count")
    stride_seconds = training.get("window_stride_seconds")
    window_seconds = window.get("window_seconds")
    if (
        isinstance(minimum_window_count, bool)
        or not isinstance(minimum_window_count, int)
        or minimum_window_count < 50
    ):
        raise ValueError("minimum_window_count must be an integer of at least 50")
    if (
        isinstance(stride_seconds, bool)
        or not isinstance(stride_seconds, int)
        or stride_seconds <= 0
    ):
        raise ValueError("window_stride_seconds must be a positive integer")
    if stride_seconds != window_seconds:
        raise ValueError("window_stride_seconds must equal window.window_seconds")
    if not training.get("baseline_fixture_path"):
        raise ValueError("baseline_fixture_path is required")
    return training


def load_normal_baseline(fixture_path: str | Path) -> list[RawFeatures]:
    """Read, parse, and extract every fixed-baseline log entry."""
    reader = LogReader(str(fixture_path))
    parser = LogParser()
    extractor = FeatureExtractor()
    features: list[RawFeatures] = []

    for line_number, raw_line in enumerate(reader.read_all(), start=1):
        parsed = parser.parse(raw_line)
        if parsed is None:
            raise ValueError(f"invalid baseline log line: {line_number}")
        features.append(extractor.extract_one(parsed))

    if not features:
        raise ValueError("normal baseline fixture contains no valid log entries")
    return features


def build_training_vectors(
    config: dict[str, Any], entries: list[RawFeatures]
) -> list[WindowFeatureVector]:
    """Construct non-overlapping event-time window feature vectors."""
    training = _training_config(config)
    window = config["window"]
    aggregator = WindowFeatureAggregator(
        window_seconds=window["window_seconds"],
        min_log_count=window["min_log_count"],
    )

    timed_entries: list[tuple[datetime, RawFeatures]] = []
    for entry in entries:
        timestamp = WindowFeatureAggregator._parse_timestamp(entry.raw_timestamp)
        if timestamp is None:
            raise ValueError("baseline feature is missing an event timestamp")
        timed_entries.append((timestamp, entry))
    timed_entries.sort(key=lambda item: item[0])

    first_timestamp = timed_entries[0][0]
    buckets: dict[int, list[RawFeatures]] = defaultdict(list)
    for timestamp, entry in timed_entries:
        offset_seconds = (timestamp - first_timestamp).total_seconds()
        bucket_index = int(offset_seconds // training["window_stride_seconds"])
        buckets[bucket_index].append(entry)

    vectors: list[WindowFeatureVector] = []
    for bucket_index in sorted(buckets):
        window_entries = buckets[bucket_index]
        if not aggregator.has_enough(window_entries):
            raise ValueError(
                "baseline window "
                f"{bucket_index} contains fewer than window.min_log_count entries"
            )
        vector = aggregator.aggregate(window_entries)
        if not isinstance(vector, WindowFeatureVector):
            raise TypeError("window aggregation did not produce a WindowFeatureVector")
        if len(vector.to_list()) != 23:
            raise ValueError("window feature dimension must be 23")
        vectors.append(vector)

    if len(vectors) < training["minimum_window_count"]:
        raise ValueError(
            "baseline contains "
            f"{len(vectors)} training windows; requires at least "
            f"{training['minimum_window_count']}"
        )
    return vectors


def train_log_model(config: dict[str, Any]) -> tuple[object, list[WindowFeatureVector]]:
    """Train, persist, reload, and validate the configured log model artifact."""
    training = _training_config(config)
    entries = load_normal_baseline(training["baseline_fixture_path"])
    vectors = build_training_vectors(config, entries)
    model = ModelTrainer(config).train(vectors)

    predictor = AnomalyPredictor(config)
    predictor.load()
    loaded_model = predictor._model
    if loaded_model is None:
        raise ValueError("trained model could not be loaded")
    if not callable(getattr(loaded_model, "predict", None)):
        raise TypeError("trained model does not provide callable predict()")
    if not callable(getattr(loaded_model, "decision_function", None)):
        raise TypeError("trained model does not provide callable decision_function()")
    if loaded_model.n_features_in_ != 23:
        raise ValueError("trained model feature dimension must be 23")
    return model, vectors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/event_detection.yml")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        _, vectors = train_log_model(config)
    except Exception as exc:
        print(f"Training failed: {exc}", file=sys.stderr)
        return 1

    print(f"Model Path: {config['output']['model_path']}")
    print(f"Training Window Count: {len(vectors)}")
    print("Feature Count: 23")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
