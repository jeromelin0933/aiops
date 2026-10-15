"""External-observer endpoint and latency mechanics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import ceil
from statistics import median
from typing import Protocol

from .campaign import ExecutionDisposition, ExecutionResult, UpstreamOutcome


@dataclass(frozen=True, slots=True)
class ExternalObservation:
    observed_at_utc: str
    event_has_unique_terminal_incident_owner: bool
    incident_durable_readable: bool
    incident_membership_coherent: bool
    artifact_durable: bool = False
    version_publication_successful: bool = False
    target_version_id: str | None = None
    candidate_a_current_version_id: str | None = None
    incident_rca_version_id: str | None = None
    upstream: UpstreamOutcome = UpstreamOutcome()

    @property
    def actionable_incident(self) -> bool:
        return (
            self.event_has_unique_terminal_incident_owner
            and self.incident_durable_readable
            and self.incident_membership_coherent
        )

    @property
    def usable_rca(self) -> bool:
        return (
            self.actionable_incident
            and self.artifact_durable
            and self.version_publication_successful
            and self.target_version_id is not None
            and self.candidate_a_current_version_id == self.target_version_id
            and self.incident_rca_version_id == self.target_version_id
        )


class ExternalObserver(Protocol):
    def observe(self, execution_id: str) -> ExternalObservation: ...


class ObservationClock(Protocol):
    def monotonic_ns(self) -> int: ...
    def utc_now(self) -> str: ...
    def wait_ms(self, milliseconds: int) -> None: ...


@dataclass(frozen=True, slots=True)
class LatencyTrace:
    signal_started_at: str
    signal_started_ns: int
    polling_interval_ms: int
    cutoff_ms: int
    actionable_incident_observed_at: str | None = None
    actionable_incident_ns: int | None = None
    usable_rca_observed_at: str | None = None
    usable_rca_ns: int | None = None

    @property
    def latency_ms(self) -> int | None:
        if self.usable_rca_ns is None:
            return None
        return max(0, (self.usable_rca_ns - self.signal_started_ns) // 1_000_000)


class LatencyKind(str, Enum):
    PRIMARY_FULL_E2E = "PRIMARY_FULL_E2E"
    REFRESH = "REFRESH"
    RESTART_RECOVERY = "RESTART_RECOVERY"


class LatencyDisposition(str, Enum):
    OBSERVED_COMPLETION = "OBSERVED_COMPLETION"
    TIMEOUT = "TIMEOUT"
    RIGHT_CENSORED = "RIGHT_CENSORED"
    FAILURE = "FAILURE"
    INVALID_EVALUATION = "INVALID_EVALUATION"


@dataclass(frozen=True, slots=True)
class LatencyMeasurement:
    kind: LatencyKind
    disposition: LatencyDisposition
    start_boundary: str
    end_boundary: str
    clock_source: str
    polling_interval_ms: int
    cutoff_ms: int
    started_at_utc: str
    ended_at_utc: str | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        for value in (self.start_boundary, self.end_boundary, self.clock_source,
                      self.started_at_utc):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("latency boundary/clock facts must be explicit")
        if self.polling_interval_ms <= 0 or self.cutoff_ms <= 0:
            raise ValueError("polling interval and cutoff must be positive")
        observed = self.disposition is LatencyDisposition.OBSERVED_COMPLETION
        if observed != (self.duration_ms is not None and self.ended_at_utc is not None):
            raise ValueError("only observed completion may carry latency duration/end")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("latency duration cannot be negative")


@dataclass(frozen=True, slots=True)
class LatencyDistribution:
    kind: LatencyKind
    observed_n: int
    minimum_ms: int | None
    median_ms: float | None
    p95_ms: int | None
    maximum_ms: int | None
    timeout_count: int
    censored_count: int
    failure_count: int
    invalid_count: int
    start_boundaries: tuple[str, ...]
    end_boundaries: tuple[str, ...]
    polling_intervals_ms: tuple[int, ...]
    cutoffs_ms: tuple[int, ...]
    estimability: str


def summarize_latency(
    kind: LatencyKind, measurements: tuple[LatencyMeasurement, ...],
) -> LatencyDistribution:
    if not isinstance(kind, LatencyKind):
        raise TypeError("kind must be a LatencyKind")
    if any(item.kind is not kind for item in measurements):
        raise ValueError("primary, refresh, and restart latency cannot be mixed")
    samples = tuple(
        item.duration_ms for item in measurements
        if item.disposition is LatencyDisposition.OBSERVED_COMPLETION
        and item.duration_ms is not None
    )
    ordered = sorted(samples)
    p95 = ordered[ceil(0.95 * len(ordered)) - 1] if ordered else None
    if not samples:
        estimability = "NOT ESTIMABLE — NO OBSERVED COMPLETIONS; > CUTOFF MAY APPLY"
    elif len(samples) < 20:
        estimability = "OBSERVED COMPLETIONS ONLY — SMALL-SAMPLE LIMITATION"
    else:
        estimability = "OBSERVED COMPLETIONS ONLY — ESTIMABLE"
    return LatencyDistribution(
        kind, len(samples), min(samples) if samples else None,
        float(median(samples)) if samples else None, p95,
        max(samples) if samples else None,
        sum(item.disposition is LatencyDisposition.TIMEOUT for item in measurements),
        sum(item.disposition in {
            LatencyDisposition.TIMEOUT, LatencyDisposition.RIGHT_CENSORED,
        } for item in measurements),
        sum(item.disposition is LatencyDisposition.FAILURE for item in measurements),
        sum(item.disposition is LatencyDisposition.INVALID_EVALUATION for item in measurements),
        tuple(sorted({item.start_boundary for item in measurements})),
        tuple(sorted({item.end_boundary for item in measurements})),
        tuple(sorted({item.polling_interval_ms for item in measurements})),
        tuple(sorted({item.cutoff_ms for item in measurements})), estimability,
    )


def primary_latency_measurements(
    results: tuple[ExecutionResult, ...], *, polling_interval_ms: int,
    cutoff_ms: int, clock_source: str,
) -> tuple[LatencyMeasurement, ...]:
    """Project runner results into the independent primary E2E distribution."""
    measurements = []
    for result in results:
        if result.signal_started_at is None:
            raise ValueError("admitted primary execution lacks signal start observation")
        if result.disposition in {
            ExecutionDisposition.OBSERVED_COMPLETION,
            ExecutionDisposition.VALID_DEGRADED_COMPLETION,
        }:
            disposition = LatencyDisposition.OBSERVED_COMPLETION
            ended_at = result.usable_rca_observed_at
            duration = result.latency_ms
        elif result.disposition is ExecutionDisposition.RIGHT_CENSORED_UNFINISHED:
            disposition = LatencyDisposition.TIMEOUT
            ended_at = None
            duration = None
        elif result.disposition is ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE:
            disposition = LatencyDisposition.FAILURE
            ended_at = None
            duration = None
        else:
            disposition = LatencyDisposition.INVALID_EVALUATION
            ended_at = None
            duration = None
        measurements.append(LatencyMeasurement(
            LatencyKind.PRIMARY_FULL_E2E, disposition,
            "EVALUATION_SIGNAL_PUBLICLY_OBSERVABLE",
            "USABLE_CURRENT_RCA_PUBLICLY_OBSERVED", clock_source,
            polling_interval_ms, result.cutoff_ms or cutoff_ms,
            result.signal_started_at, ended_at, duration,
        ))
    return tuple(measurements)
