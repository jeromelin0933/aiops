"""Scenario Runtime Foundation for SPEC-005.

This package owns generator *state* only.  It neither imports nor runs event
detection components, and it never creates events, alerts, or incidents.
"""

from .config import ScenarioConfig, ScenarioConfigLoader
from .runtime import MockDataRuntime
from .schema import (
    ScenarioCommand,
    ScenarioId,
    ScenarioPhase,
    ScenarioRuntimeSnapshot,
    ScenarioTriggerResult,
    ScenarioValidationResult,
)

__all__ = [
    "GeneratorAdapter", "MockDataRuntime", "ScenarioCommand", "ScenarioConfig",
    "ScenarioConfigLoader", "ScenarioId", "ScenarioPhase",
    "ScenarioRuntimeSnapshot", "ScenarioTriggerResult",
    "ScenarioValidationResult",
]


def __getattr__(name: str):
    """Lazily expose optional generator integration without package-init cycles."""
    if name == "GeneratorAdapter":
        from .integration import GeneratorAdapter
        return GeneratorAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
