"""Train and validate the fixed-baseline SPEC-003 Metrics Isolation Forest model."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.event_detection.metrics_iforest import (  # noqa: E402
    MetricsIForestConfigLoader,
    MetricsIForestModelLoader,
    MetricsIForestTrainer,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/metrics_iforest.yaml")
    args = parser.parse_args()

    try:
        config = MetricsIForestConfigLoader.load(args.config)
        trainer = MetricsIForestTrainer(config)
        artifact = trainer.train_from_fixture(config["training"]["baseline_fixture_path"])
        trainer.save_artifact(artifact, config["model"]["path"])
        MetricsIForestModelLoader(config).load(config["model"]["path"])
    except Exception as exc:  # The script must return a non-zero exit code on any validation failure.
        print(f"Training failed: {exc}", file=sys.stderr)
        return 1

    print(f"Model Path: {config['model']['path']}")
    print(f"Training Window Count: {artifact['training_window_count']}")
    print(f"Feature Count: {len(artifact['feature_names'])}")
    print(f"Metadata Version: {artifact['metadata_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
