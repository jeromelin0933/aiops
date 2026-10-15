"""Deterministic denominator, replacement, and latency aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from random import Random
from statistics import median

from .campaign import ApprovedMetricDisposition, ExecutionDisposition, ExecutionResult


BOOTSTRAP_ALGORITHM = "PERCENTILE_BOOTSTRAP_V1"
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 17017


@dataclass(frozen=True, slots=True)
class ScenarioAggregate:
    scenario_id: str
    planned: int
    quality_eligible: int
    successes: int
    quality_rate: float | None
    production_failures: int
    degraded: int
    censored: int
    invalid: int
    observed_latencies_ms: tuple[int, ...]
    disposition_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class CampaignAggregate:
    planned: int
    quality_eligible: int
    successes: int
    overall_micro_rate: float | None
    scenario_equal_macro_rate: float | None
    production_failures: int
    degraded: int
    censored: int
    invalid: int
    replacement_executions: int
    observed_latency_count: int
    median_latency_ms: float | None
    p95_latency_ms: int | None
    min_latency_ms: int | None
    max_latency_ms: int | None
    latency_estimability: str
    micro_95_interval: tuple[float, float] | None
    macro_95_interval: tuple[float, float] | None
    bootstrap_algorithm: str
    bootstrap_resamples: int
    bootstrap_seed: int
    bootstrap_limitation: str
    disposition_counts: tuple[tuple[str, int], ...]
    per_scenario: tuple[ScenarioAggregate, ...]
    diagnostic_stage_waterfall: tuple[tuple[str, int], ...]


def nearest_rank(values: tuple[int, ...], percentile: int) -> int | None:
    if not values:
        return None
    if type(percentile) is not int or not 1 <= percentile <= 100:
        raise ValueError("percentile must be an integer from 1 through 100")
    ordered = sorted(values)
    return ordered[ceil(percentile * len(ordered) / 100) - 1]


def _percentile_interval(values: list[float]) -> tuple[float, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    lower = ordered[max(0, ceil(0.025 * len(ordered)) - 1)]
    upper = ordered[max(0, ceil(0.975 * len(ordered)) - 1)]
    return (lower, upper)


def _bootstrap_intervals(
    per_scenario_outcomes: tuple[tuple[int, ...], ...], *,
    seed: int = BOOTSTRAP_SEED, resamples: int = BOOTSTRAP_RESAMPLES,
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Deterministic percentile bootstrap of §8.4 binary slot outcomes."""
    if type(seed) is not int or type(resamples) is not int or resamples < 1:
        raise ValueError("bootstrap seed/resamples are invalid")
    if not per_scenario_outcomes or any(not values for values in per_scenario_outcomes):
        return None, None
    random = Random(seed)
    flat = tuple(value for values in per_scenario_outcomes for value in values)
    micro: list[float] = []
    macro: list[float] = []
    for _ in range(resamples):
        micro_sample = tuple(flat[random.randrange(len(flat))] for _ in flat)
        micro.append(sum(micro_sample) / len(micro_sample))
        scenario_rates = []
        for values in per_scenario_outcomes:
            sample = tuple(values[random.randrange(len(values))] for _ in values)
            scenario_rates.append(sum(sample) / len(sample))
        macro.append(sum(scenario_rates) / len(scenario_rates))
    return _percentile_interval(micro), _percentile_interval(macro)


def _slot_outcomes(
    results: tuple[ExecutionResult, ...], expected_ids: tuple[str, ...],
) -> tuple[tuple[ExecutionResult, ...], tuple[ExecutionResult, ...]]:
    if not expected_ids or len(set(expected_ids)) != len(expected_ids):
        raise ValueError("expected planned denominator must be non-empty and unique")
    by_execution = {item.execution.execution_id: item for item in results}
    if len(by_execution) != len(results):
        raise ValueError("execution results must have unique immutable identities")
    missing = set(expected_ids) - set(by_execution)
    if missing:
        raise ValueError("results must preserve every original planned execution")
    extras = tuple(item for item in results if item.execution.execution_id not in expected_ids)
    for item in extras:
        original_id = item.execution.replacement_for_execution_id
        if original_id not in expected_ids:
            raise ValueError("extra execution cannot masquerade as a replacement")
        raise ValueError(
            "Approved SPEC-017 does not authorize replacement of an admitted execution"
        )
    # Approved section 8.4 fixes the denominator and outcome to each admitted
    # scheduled execution.  A diagnostic rerun remains append-only lineage; it
    # can never promote or rewrite the original slot's metric truth.
    return tuple(by_execution[execution_id] for execution_id in expected_ids), extras


def aggregate(results: tuple[ExecutionResult, ...], *,
              expected_execution_ids: tuple[str, ...]) -> CampaignAggregate:
    selected, replacements = _slot_outcomes(results, expected_execution_ids)
    all_invalid = sum(item.disposition is ExecutionDisposition.INVALID_EVALUATION
                      for item in results)

    def disposition_count(items: tuple[ExecutionResult, ...],
                          disposition: ExecutionDisposition) -> int:
        return sum(item.disposition is disposition for item in items)

    # Approved §8.4 denominator is every admitted scheduled nominal slot.
    # Replacement lineage is diagnostic only: original slot truth remains in
    # both numerator and denominator and the rerun creates no second slot.
    quality = selected
    latencies = tuple(item.latency_ms for item in selected if item.latency_ms is not None)
    median_value = float(median(latencies)) if latencies else None
    p95_value = nearest_rank(latencies, 95)
    if not latencies:
        estimability = "NOT ESTIMABLE — NO OBSERVED COMPLETIONS"
    elif len(latencies) < 20:
        estimability = "OBSERVED COMPLETIONS ONLY — SMALL-SAMPLE LIMITATION"
    else:
        estimability = "OBSERVED COMPLETIONS ONLY — ESTIMABLE"

    scenario_rows = []
    for scenario in sorted({item.execution.scenario_id for item in selected}):
        items = tuple(item for item in selected if item.execution.scenario_id == scenario)
        eligible = items
        successes = sum(item.rca_success for item in eligible)
        disposition_counts = tuple(
            (disposition.value, sum(
                item.metric_disposition is disposition for item in items))
            for disposition in ApprovedMetricDisposition
        )
        scenario_rows.append(ScenarioAggregate(
            scenario, len(items), len(eligible), successes,
            successes / len(eligible) if eligible else None,
            disposition_count(items, ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE),
            disposition_count(items, ExecutionDisposition.VALID_DEGRADED_COMPLETION),
            disposition_count(items, ExecutionDisposition.RIGHT_CENSORED_UNFINISHED),
            sum(item.disposition is ExecutionDisposition.INVALID_EVALUATION for item in results
                if item.execution.scenario_id == scenario),
            tuple(item.latency_ms for item in items if item.latency_ms is not None),
            disposition_counts,
        ))
    rates = tuple(item.quality_rate for item in scenario_rows)
    stages: dict[str, int] = {}
    for item in results:
        stages[item.diagnostic_stage] = stages.get(item.diagnostic_stage, 0) + 1
    successes = sum(item.rca_success for item in quality)
    disposition_counts = tuple(
        (disposition.value, sum(
            item.metric_disposition is disposition for item in selected))
        for disposition in ApprovedMetricDisposition
    )
    micro_interval, macro_interval = _bootstrap_intervals(tuple(
        tuple(1 if item.rca_success else 0 for item in selected
              if item.execution.scenario_id == row.scenario_id)
        for row in scenario_rows
    ))
    return CampaignAggregate(
        planned=len(expected_execution_ids), quality_eligible=len(quality), successes=successes,
        overall_micro_rate=successes / len(quality) if quality else None,
        scenario_equal_macro_rate=sum(rates) / len(rates) if rates else None,
        production_failures=disposition_count(selected, ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE),
        degraded=disposition_count(selected, ExecutionDisposition.VALID_DEGRADED_COMPLETION),
        censored=disposition_count(selected, ExecutionDisposition.RIGHT_CENSORED_UNFINISHED),
        invalid=all_invalid, replacement_executions=len(replacements),
        observed_latency_count=len(latencies), median_latency_ms=median_value,
        p95_latency_ms=p95_value,
        min_latency_ms=min(latencies) if latencies else None,
        max_latency_ms=max(latencies) if latencies else None,
        latency_estimability=estimability,
        micro_95_interval=micro_interval, macro_95_interval=macro_interval,
        bootstrap_algorithm=BOOTSTRAP_ALGORITHM,
        bootstrap_resamples=BOOTSTRAP_RESAMPLES, bootstrap_seed=BOOTSTRAP_SEED,
        bootstrap_limitation=(
            "DESCRIPTIVE ONLY — SMALL-N; RAW COUNTS REMAIN AUTHORITATIVE"
            if len(selected) < 30 else
            "DESCRIPTIVE INTERVAL; RAW COUNTS REMAIN AUTHORITATIVE"
        ),
        disposition_counts=disposition_counts,
        per_scenario=tuple(scenario_rows),
        diagnostic_stage_waterfall=tuple(sorted(stages.items())),
    )
