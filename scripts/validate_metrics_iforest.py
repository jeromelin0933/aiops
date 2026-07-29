"""Validate the trained SPEC-003 Metrics Isolation Forest against fixed fixtures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.event_detection import metrics_iforest as metrics_module  # noqa: E402
from src.event_detection.metrics_iforest import (  # noqa: E402
    MetricsIForestConfigLoader,
    MetricsIForestDetector,
    MetricsIForestModelLoader,
    MetricsWindowFeatures,
)


FIXTURE_DIRECTORY = PROJECT_ROOT / "tests" / "fixtures" / "metrics_iforest"
FIXTURE_EXPECTATIONS = {
    "prometheus_qps_normal.json": None,
    "prometheus_qps_spike.json": "request_spike_detected",
    "prometheus_qps_general_anomaly.json": "general_metrics_anomaly",
    "prometheus_qps_empty.json": None,
}


class _FixtureResponse:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class _MemoryEventStore:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURE_DIRECTORY / name
    with path.open(encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def _validate_artifact(config: dict[str, Any]) -> dict[str, Any]:
    artifact = MetricsIForestModelLoader(config).load(config["model"]["path"])
    if artifact["metadata_version"] != config["model"]["metadata_version"]:
        raise AssertionError("artifact metadata_version does not match config")
    if artifact["metric_name"] != config["metric"]["name"]:
        raise AssertionError("artifact metric_name does not match config")
    if artifact["feature_names"] != MetricsWindowFeatures.feature_names():
        raise AssertionError("artifact feature contract does not match runtime")
    return artifact


def _run_fixture(config_path: str, fixture_name: str) -> list[dict[str, Any]]:
    payload = _load_fixture(fixture_name)
    store = _MemoryEventStore()
    original_get = metrics_module.requests.get
    metrics_module.requests.get = lambda *_args, **_kwargs: _FixtureResponse(payload)
    try:
        detector = MetricsIForestDetector(config_path, event_store=store)
        detector.time_provider = lambda: 1720000300.0
        events = detector.run_once()
    finally:
        metrics_module.requests.get = original_get

    if store.events != events:
        raise AssertionError(f"{fixture_name}: EventStore output does not match detector output")
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/metrics_iforest.yaml")
    args = parser.parse_args()

    try:
        config = MetricsIForestConfigLoader.load(args.config)
        artifact = _validate_artifact(config)
        print(
            "Artifact: PASS "
            f"(metadata_version={artifact['metadata_version']}, "
            f"training_window_count={artifact['training_window_count']}, "
            f"feature_count={len(artifact['feature_names'])})"
        )

        for fixture_name, expected_event_type in FIXTURE_EXPECTATIONS.items():
            events = _run_fixture(args.config, fixture_name)
            if expected_event_type is None:
                if events:
                    raise AssertionError(f"{fixture_name}: expected no formal event, got {events}")
                print(f"{fixture_name}: PASS (no formal event)")
                continue

            if len(events) != 1 or events[0]["event_type"] != expected_event_type:
                raise AssertionError(
                    f"{fixture_name}: expected one {expected_event_type} event, got {events}"
                )
            event = events[0]
            if len(event) != 15:
                raise AssertionError(f"{fixture_name}: event must contain exactly 15 top-level fields")
            json.dumps(event, ensure_ascii=False, allow_nan=False)
            print(f"{fixture_name}: PASS ({expected_event_type}, 15 fields, JSON finite)")
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1

    print("Metrics IForest validation: PASS")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
