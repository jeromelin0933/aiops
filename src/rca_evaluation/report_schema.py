"""Immutable canonical schemas for SPEC-017 S6 report projections."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .identity import commitment


REPORT_SCHEMA_VERSION = "SPEC-017-FORMAL-REPORT-v1"
EVIDENCE_INDEX_SCHEMA_VERSION = "SPEC-017-EVIDENCE-INDEX-v1"
READINESS_SCHEMA_VERSION = "SPEC-017-READINESS-v1"


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    domain: str
    record_id: str
    parent_id: str | None
    semantic_commitment: str
    evidence_kind: str
    safe_references: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceIndex:
    schema_version: str
    evaluation_run_id: str
    evaluation_plan_id: str
    oracle_revision_id: str
    oracle_content_commitment: str | None
    repository_identity: str | None
    fixture_revision: str | None
    manifest_commitment: str | None
    source_cut_commitment: str
    entries: tuple[EvidenceEntry, ...]

    @property
    def evidence_index_commitment(self) -> str:
        return commitment(asdict(self))


@dataclass(frozen=True, slots=True)
class MetricProjection:
    name: str
    numerator: int
    denominator: int
    rate_fraction: tuple[int, int] | None
    macro_primary_rate_fraction: tuple[int, int] | None
    micro_supplemental_rate_fraction: tuple[int, int] | None
    invalid_count: int
    failure_count: int
    censor_count: int
    missing_count: int
    micro_95_interval: tuple[str, str] | None = None
    macro_95_interval: tuple[str, str] | None = None
    status: str = "COMPLETE"


@dataclass(frozen=True, slots=True)
class ScenarioMetricProjection:
    scenario_id: str
    root_cause: MetricProjection
    unsupported_claims: MetricProjection
    conclusion_calibration: MetricProjection


@dataclass(frozen=True, slots=True)
class LatencyProjection:
    kind: str
    observed_count: int
    failure_count: int
    censor_count: int
    timeout_count: int
    invalid_count: int
    missing_count: int
    minimum_ms: int | None
    median_ms_fraction: tuple[int, int] | None
    p95_ms: int | None
    maximum_ms: int | None
    start_boundaries: tuple[str, ...]
    end_boundaries: tuple[str, ...]
    polling_intervals_ms: tuple[int, ...]
    cutoffs_ms: tuple[int, ...]
    estimability: str


@dataclass(frozen=True, slots=True)
class GateProjection:
    gate: str
    state: str
    formal_pass: bool
    authorization_ids: tuple[str, ...]
    invocation_start_ids: tuple[str, ...]
    terminal_receipt_ids: tuple[str, ...]
    legacy_receipt_ids: tuple[str, ...]
    docker_namespace_ids: tuple[str, ...]
    docker_cleanup_ids: tuple[str, ...]
    profile_pins: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ExecutionProjection:
    execution_id: str
    scenario_id: str
    repetition: int
    planned_slot_execution_id: str
    replacement_for_execution_id: str | None
    disposition: str | None
    metric_disposition: str | None
    root_cause_success: bool | None
    unsupported_claim_numerator: int | None
    unsupported_claim_denominator: int | None
    contradicted_claim_count: int | None
    conclusion_calibration_valid: bool | None
    knowledge_result: str | None
    failure_classification: str | None
    diagnostic_stage: str | None
    primary_latency_disposition: str
    refresh_latency_disposition: str
    restart_latency_disposition: str
    applicable_real_gate_states: tuple[tuple[str, str], ...]
    evidence_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportComponentProjection:
    """Traceable §13.1 result/distribution, including explicit absence."""

    name: str
    outcome_counts: tuple[tuple[str, int], ...]
    evidence_record_ids: tuple[str, ...]
    complete: bool
    passed: bool
    required_case_ids: tuple[str, ...] = ()
    observed_case_ids: tuple[str, ...] = ()
    cases: tuple["CaseResultProjection", ...] = ()


@dataclass(frozen=True, slots=True)
class CaseResultProjection:
    case_id: str
    execution_id: str
    disposition: str
    before_state: str | None
    trigger_basis: str | None
    resulting_state: str | None
    freshness_transition: str | None
    prior_current_preserved: bool | None
    recovered_identity: str | None
    duplicate_effect_detected: bool | None
    publication_continuity: str | None
    latency_disposition: str | None
    evidence_record_id: str


@dataclass(frozen=True, slots=True)
class JudgmentProjection:
    judgment_id: str
    execution_id: str
    oracle_revision_id: str
    result: str
    semantic_commitment: str


@dataclass(frozen=True, slots=True)
class ReAdjudicationProjection:
    re_adjudication_id: str
    original_judgment_id: str
    original_oracle_revision_id: str
    new_oracle_revision_id: str
    authorization_reference: str
    result: str
    semantic_commitment: str


@dataclass(frozen=True, slots=True)
class ReadinessProjection:
    schema_version: str
    engineering_implementation_complete: bool
    engineering_complete: bool
    evaluation_ready: bool
    evaluation_executed: bool
    formal_evaluation_pass: bool
    product_acceptance_complete: bool
    production_ready: bool
    blockers: tuple[str, ...]
    basis_source_cut_commitment: str


@dataclass(frozen=True, slots=True)
class FormalReport:
    schema_version: str
    evaluation_run_id: str
    evaluation_plan_id: str
    oracle_revision_id: str
    source_cut_commitment: str
    evidence_index: EvidenceIndex
    executions: tuple[ExecutionProjection, ...]
    original_judgments: tuple[JudgmentProjection, ...]
    authorized_re_adjudications: tuple[ReAdjudicationProjection, ...]
    re_adjudication_ambiguous: bool
    root_cause_metric: MetricProjection
    unsupported_claim_metric: MetricProjection
    conclusion_calibration_metric: MetricProjection
    contradicted_claim_count: int
    knowledge_distribution: ReportComponentProjection
    degraded_outcome_distribution: ReportComponentProjection
    isolation_results: ReportComponentProjection
    deviation_results: ReportComponentProjection
    abort_incomplete_results: ReportComponentProjection
    refresh_version_results: ReportComponentProjection
    restart_recovery_results: ReportComponentProjection
    per_scenario: tuple[ScenarioMetricProjection, ...]
    latency: tuple[LatencyProjection, ...]
    real_gates: tuple[GateProjection, ...]
    readiness: ReadinessProjection

    @property
    def report_semantic_commitment(self) -> str:
        return commitment(asdict(self))
