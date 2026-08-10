"""Control-plane data structures for the SPEC-005 scenario runtime."""

from dataclasses import dataclass
from enum import Enum


class ScenarioId(str, Enum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"


class ScenarioPhase(str, Enum):
    STOPPED = "STOPPED"
    BASELINE = "BASELINE"
    INJECTING = "INJECTING"
    RECOVERY = "RECOVERY"


@dataclass(frozen=True)
class ScenarioCommand:
    scenario_id: ScenarioId
    requested_at: float


@dataclass(frozen=True)
class ScenarioRuntimeSnapshot:
    phase: ScenarioPhase
    active_scenario: ScenarioId | None
    phase_started_at: float
    phase_ends_at: float | None
    trigger_count: int
    stop_requested: bool


@dataclass(frozen=True)
class ScenarioTriggerResult:
    accepted: bool
    reason: str
    snapshot: ScenarioRuntimeSnapshot


@dataclass(frozen=True)
class ScenarioValidationResult:
    scenario_id: str
    started_at: str
    completed_at: str
    expected_event_types: list[str]
    actual_event_types: list[str]
    missing_event_types: list[str]
    unexpected_event_types: list[str]
    event_schema_valid: bool
    generator_evidence_valid: bool
    prometheus_evidence_valid: bool
    runner_success: bool
    passed: bool
    failure_reason: str | None
