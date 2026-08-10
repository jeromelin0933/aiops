"""Narrow generator adapter used by the mock runtime CLI.

This layer is intentionally one-way: it gives runtime state to generators and
does not import any event-detection component.
"""

from __future__ import annotations

import random

from src.log_generator.log_generator import LogGenerator
from src.metrics_generator.metrics_generator import MetricsGenerator

from .config import ScenarioConfig
from .schema import ScenarioRuntimeSnapshot


class GeneratorAdapter:
    """Keep log and metric generation connected to the same runtime tick and RNG."""

    def __init__(self, config: ScenarioConfig) -> None:
        self.log_generator = LogGenerator(config)
        self.metrics_generator = MetricsGenerator(config)

    def start(self) -> None:
        self.metrics_generator.start_exporter()

    def tick(self, snapshot: ScenarioRuntimeSnapshot, rng: random.Random) -> None:
        self.log_generator.tick(snapshot, rng)
        self.metrics_generator.tick(snapshot, rng)
