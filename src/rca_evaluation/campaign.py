"""Precommitted finite campaign and typed execution dispositions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rca_persistence.contracts import EvidenceCompleteness

from .fixtures import (
    AdmittedFixture, FixtureAdmissionRequest, VerifiedFixtureResolver,
    REQUIRED_RESTART_BOUNDARIES, require_verified_fixture_resolver,
)
from .identity import identity, require_identity
from .oracle import EvaluationPlan, SCENARIOS


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


class ExecutionDisposition(str, Enum):
    OBSERVED_COMPLETION = "OBSERVED_COMPLETION"
    VALID_DEGRADED_COMPLETION = "VALID_DEGRADED_COMPLETION"
    TERMINAL_PRODUCTION_FAILURE = "TERMINAL_PRODUCTION_FAILURE"
    RIGHT_CENSORED_UNFINISHED = "RIGHT_CENSORED_UNFINISHED"
    INVALID_EVALUATION = "INVALID_EVALUATION"


class ApprovedMetricDisposition(str, Enum):
    """Approved SPEC-017 §8.4 denominator vocabulary."""

    PASS = "PASS"
    RCA_FAIL = "RCA_FAIL"
    SYSTEM_FAILURE = "SYSTEM_FAILURE"
    TIMEOUT = "TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    INVALID_OBSERVATION = "INVALID_OBSERVATION"
    ISOLATION_BREACH = "ISOLATION_BREACH"
    INVALID_EVALUATION = "INVALID_EVALUATION"
    NOT_STARTED = "NOT_STARTED"


class KnowledgeDisposition(str, Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    RETRIEVAL_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"
    INVALID = "INVALID"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


class FailureCase(str, Enum):
    NONE = "NONE"
    LOKI_UNAVAILABLE = "LOKI_UNAVAILABLE"
    PROMETHEUS_UNAVAILABLE = "PROMETHEUS_UNAVAILABLE"
    PARTIAL_EVIDENCE = "PARTIAL_EVIDENCE"
    KNOWLEDGE_NO_MATCH = "KNOWLEDGE_NO_MATCH"
    KNOWLEDGE_RETRIEVAL_UNAVAILABLE = "KNOWLEDGE_RETRIEVAL_UNAVAILABLE"
    LLM_TIMEOUT_OR_FAILURE = "LLM_TIMEOUT_OR_FAILURE"
    RETRY_EXHAUSTION = "RETRY_EXHAUSTION"
    SCHEMA_OR_VALIDATION_FAILURE = "SCHEMA_OR_VALIDATION_FAILURE"
    DEGRADED_ARTIFACT = "DEGRADED_ARTIFACT"
    REFRESH_FAILURE = "REFRESH_FAILURE"
    AUTHORITATIVE_NON_SUCCESS = "AUTHORITATIVE_NON_SUCCESS"


class AuthoritativeOutcomeStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    TERMINAL_NON_SUCCESS = "TERMINAL_NON_SUCCESS"


_TYPED_FAILURE_CASES = frozenset({
    FailureCase.LLM_TIMEOUT_OR_FAILURE,
    FailureCase.RETRY_EXHAUSTION,
    FailureCase.SCHEMA_OR_VALIDATION_FAILURE,
    FailureCase.REFRESH_FAILURE,
    FailureCase.AUTHORITATIVE_NON_SUCCESS,
})
_DEGRADED_CASES = frozenset({
    FailureCase.LOKI_UNAVAILABLE,
    FailureCase.PROMETHEUS_UNAVAILABLE,
    FailureCase.PARTIAL_EVIDENCE,
    FailureCase.KNOWLEDGE_RETRIEVAL_UNAVAILABLE,
    FailureCase.DEGRADED_ARTIFACT,
})


def validate_knowledge_consistency(
    knowledge: KnowledgeDisposition, failure_case: FailureCase,
) -> None:
    """Fail closed unless the two Knowledge projections agree bidirectionally."""
    expected_case = {
        KnowledgeDisposition.NO_MATCH: FailureCase.KNOWLEDGE_NO_MATCH,
        KnowledgeDisposition.RETRIEVAL_UNAVAILABLE:
            FailureCase.KNOWLEDGE_RETRIEVAL_UNAVAILABLE,
    }.get(knowledge)
    expected_knowledge = {
        FailureCase.KNOWLEDGE_NO_MATCH: KnowledgeDisposition.NO_MATCH,
        FailureCase.KNOWLEDGE_RETRIEVAL_UNAVAILABLE:
            KnowledgeDisposition.RETRIEVAL_UNAVAILABLE,
    }.get(failure_case)
    if ((expected_case is not None and failure_case is not expected_case)
            or (expected_knowledge is not None and knowledge is not expected_knowledge)):
        raise ValueError(
            "Knowledge disposition and failure case are semantically inconsistent"
        )


@dataclass(frozen=True, slots=True)
class CampaignAdmission:
    repository_commit: str
    repository_dirty: bool
    evaluation_config_commitment: str
    provider_profile_references: tuple[str, ...]
    rag_profile_references: tuple[str, ...]
    environment_identity: str
    evaluator_rubric_version: str
    polling_interval_ms: int
    timeout_ms: int
    clock_source: str
    clock_synchronization_uncertainty_ms: int
    random_seed: int | None = None
    deterministic_order_commitment: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "repository_commit", "evaluation_config_commitment", "environment_identity",
            "evaluator_rubric_version", "clock_source",
        ):
            _text(getattr(self, name), name)
        if not self.evaluation_config_commitment.startswith("sha256:"):
            raise ValueError("evaluation_config_commitment must be a SHA-256 commitment")
        if type(self.repository_dirty) is not bool:
            raise ValueError("repository_dirty must be boolean")
        for name in ("provider_profile_references", "rag_profile_references"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not values:
                raise ValueError(f"{name} must be a non-empty tuple")
            for value in values:
                _text(value, name)
        if type(self.polling_interval_ms) is not int or self.polling_interval_ms <= 0:
            raise ValueError("polling_interval_ms must be positive")
        if type(self.timeout_ms) is not int or self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        if self.polling_interval_ms > self.timeout_ms:
            raise ValueError("polling interval cannot exceed timeout")
        if (type(self.clock_synchronization_uncertainty_ms) is not int
                or self.clock_synchronization_uncertainty_ms < 0):
            raise ValueError("clock synchronization uncertainty must be a non-negative integer")
        if self.random_seed is not None and type(self.random_seed) is not int:
            raise ValueError("random_seed must be an integer or None")
        if self.random_seed is None:
            _text(self.deterministic_order_commitment, "deterministic_order_commitment")


@dataclass(frozen=True, slots=True)
class PlannedExecution:
    execution_id: str
    scenario_id: str
    repetition: int
    input_identity: str
    input_commitment: str
    case: FailureCase = FailureCase.NONE
    replacement_for_execution_id: str | None = None

    @property
    def planned_slot_execution_id(self) -> str:
        return self.replacement_for_execution_id or self.execution_id

    @property
    def is_replacement(self) -> bool:
        return self.replacement_for_execution_id is not None


@dataclass(frozen=True, slots=True)
class PlannedRecoveryCase:
    """A fixture-admitted S4 case attached to an admitted execution slot."""

    case_id: str
    parent_execution_id: str
    fixture: AdmittedFixture

    @property
    def kind(self) -> str:
        return ("RESTART" if self.fixture.variant == "restart_boundary"
                else "REFRESH")


@dataclass(frozen=True, slots=True)
class CampaignPlan:
    evaluation_plan_id: str
    run_id: str
    campaign_key: str
    repetitions: int
    executions: tuple[PlannedExecution, ...]
    admission: CampaignAdmission
    fixture_resolver: VerifiedFixtureResolver
    fixture_requests: tuple[FixtureAdmissionRequest, ...]
    fixture_admissions: tuple[AdmittedFixture, ...]
    recovery_cases: tuple[PlannedRecoveryCase, ...]

    @classmethod
    def fixed_nominal(
        cls,
        plan: EvaluationPlan,
        *,
        campaign_key: str,
        fixture_resolver: VerifiedFixtureResolver,
        fixture_requests: dict[str, FixtureAdmissionRequest],
        admission: CampaignAdmission,
        order: tuple[tuple[str, int], ...] | None = None,
    ) -> "CampaignPlan":
        _text(campaign_key, "campaign_key")
        if not isinstance(admission, CampaignAdmission):
            raise TypeError("admission must be CampaignAdmission")
        expected = {(scenario, repetition) for repetition in range(1, plan.repetitions + 1)
                    for scenario in SCENARIOS}
        selected = order or tuple(
            (scenario, repetition)
            for repetition in range(1, plan.repetitions + 1)
            for scenario in SCENARIOS
        )
        if len(selected) != len(expected) or set(selected) != expected:
            raise ValueError("execution order must contain exactly S1-S6 x fixed N once")
        resolver = require_verified_fixture_resolver(fixture_resolver)
        if set(fixture_requests) != set(SCENARIOS):
            raise ValueError("fixture requests must cover exactly S1 through S6")
        requests = tuple(fixture_requests[item] for item in SCENARIOS)
        admitted_fixtures = tuple(resolver.admit(item) for item in requests)
        manifest_facts = {
            (
                item.manifest_identity, item.manifest_commitment,
                item.fixture_revision, item.oracle_revision_id,
                item.oracle_content_commitment,
            )
            for item in admitted_fixtures
        }
        if len(manifest_facts) != 1:
            raise ValueError("campaign fixtures must come from one official manifest")
        for scenario, item in zip(SCENARIOS, admitted_fixtures):
            if item.scenario_id != scenario or item.variant != "nominal":
                raise ValueError("formal nominal campaign requires exact nominal fixture admission")
            if item.fixture_revision != plan.fixture_revision:
                raise ValueError("fixture admission revision does not match admitted plan")
            if (item.oracle_revision_id != plan.oracle_revision_id
                    or item.oracle_content_commitment
                        != plan.oracle_content_commitment):
                raise ValueError(
                    "fixture admission Oracle commitment does not match admitted plan"
                )
        run_id = identity("evaluation_run", [plan.evaluation_plan_id, campaign_key])
        executions = tuple(
            PlannedExecution(
                identity("execution", [run_id, scenario, repetition]),
                scenario, repetition,
                fixture_requests[scenario].input_identity,
                fixture_requests[scenario].input_commitment,
            )
            for scenario, repetition in selected
        )
        by_scenario = {
            scenario: next(item for item in executions
                           if item.scenario_id == scenario)
            for scenario in SCENARIOS
        }
        required_entries = tuple(
                item for item in resolver.entries
            if ((item.scenario_id, item.variant) in {
                    ("S2", "material_refresh"), ("S2", "refresh_failure"),
                    ("S2", "non_material")}
                or (item.scenario_id == "S5"
                    and item.variant == "restart_boundary"))
        )
        restart_order = {value: index for index, value in enumerate(
            REQUIRED_RESTART_BOUNDARIES)}
        required_entries = tuple(sorted(
            required_entries,
            key=lambda item: (
                0 if item.variant == "material_refresh" else
                1 if item.variant == "refresh_failure" else
                2 if item.variant == "non_material" else 3,
                restart_order.get(item.recovery_boundary, -1),
            ),
        ))
        recovery_cases = []
        for entry in required_entries:
            admitted = resolver.admit(FixtureAdmissionRequest(
                entry.scenario_id, entry.variant, entry.input_identity,
                entry.input_commitment, entry.oracle_scenario_reference,
                entry.recovery_boundary,
            ))
            if (admitted.fixture_revision != plan.fixture_revision
                    or admitted.oracle_revision_id != plan.oracle_revision_id
                    or admitted.oracle_content_commitment
                        != plan.oracle_content_commitment):
                raise ValueError("recovery fixture does not match admitted plan")
            parent = by_scenario[entry.scenario_id].execution_id
            recovery_cases.append(PlannedRecoveryCase(
                identity("observation", [parent, "recovery-case",
                                         entry.fixture_identity]),
                parent, admitted,
            ))
        refresh_count = sum(item.kind == "REFRESH" for item in recovery_cases)
        restart_boundaries = {
            item.fixture.recovery_boundary for item in recovery_cases
            if item.kind == "RESTART"
        }
        if refresh_count != 3 or restart_boundaries != set(
                REQUIRED_RESTART_BOUNDARIES):
            raise ValueError("formal campaign required recovery cases are incomplete")
        return cls(plan.evaluation_plan_id, run_id, campaign_key,
                   plan.repetitions, executions, admission, resolver, requests,
                   admitted_fixtures, tuple(recovery_cases))

    @property
    def required_refresh_case_ids(self) -> tuple[str, ...]:
        result = tuple(item.case_id for item in self.recovery_cases
                       if item.kind == "REFRESH")
        if not result:
            raise ValueError("required refresh case set is empty")
        return result

    @property
    def required_restart_case_ids(self) -> tuple[str, ...]:
        result = tuple(item.case_id for item in self.recovery_cases
                       if item.kind == "RESTART")
        if not result:
            raise ValueError("required restart case set is empty")
        return result

    def validate(self, plan: EvaluationPlan) -> None:
        require_identity(self.run_id, "evaluation_run")
        expected = self.fixed_nominal(
            plan, campaign_key=self.campaign_key,
            fixture_resolver=self.fixture_resolver,
            fixture_requests={item.scenario_id: item for item in self.fixture_requests},
            admission=self.admission,
            order=tuple((item.scenario_id, item.repetition) for item in self.executions),
        )
        if self != expected:
            raise ValueError("campaign no longer matches its fixed admitted plan")

    def replacement_for(self, invalid_execution_id: str) -> PlannedExecution:
        del invalid_execution_id
        raise ValueError(
            "Approved SPEC-017 does not authorize replacement of an admitted execution"
        )


@dataclass(frozen=True, slots=True)
class UpstreamOutcome:
    """Projection of typed upstream facts; degraded is never caller asserted."""

    status: AuthoritativeOutcomeStatus = AuthoritativeOutcomeStatus.IN_PROGRESS
    failure_case: FailureCase = FailureCase.NONE
    failure_stage: str | None = None
    knowledge: KnowledgeDisposition = KnowledgeDisposition.MATCH
    evidence_completeness: EvidenceCompleteness = EvidenceCompleteness.FULL
    omission_facts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_knowledge_consistency(self.knowledge, self.failure_case)
        if self.failure_case in _TYPED_FAILURE_CASES and self.status not in {
            AuthoritativeOutcomeStatus.TERMINAL_FAILURE,
            AuthoritativeOutcomeStatus.TERMINAL_NON_SUCCESS,
        }:
            raise ValueError("typed failure case requires authoritative terminal non-success")
        if (self.failure_case is FailureCase.KNOWLEDGE_NO_MATCH
                and self.status is not AuthoritativeOutcomeStatus.SUCCESS):
            raise ValueError("Knowledge NO_MATCH is legal only as a successful typed result")
        if self.evidence_completeness is EvidenceCompleteness.DEGRADED:
            if self.failure_case not in _DEGRADED_CASES or not self.omission_facts:
                raise ValueError("degraded completeness requires typed degraded path and omissions")
        elif self.omission_facts:
            raise ValueError("omission facts require DEGRADED completeness")

    @property
    def terminal_failure(self) -> bool:
        return self.status in {
            AuthoritativeOutcomeStatus.TERMINAL_FAILURE,
            AuthoritativeOutcomeStatus.TERMINAL_NON_SUCCESS,
        }

    @property
    def valid_degraded_completion(self) -> bool:
        return (
            self.status is AuthoritativeOutcomeStatus.SUCCESS
            and self.evidence_completeness is EvidenceCompleteness.DEGRADED
            and self.failure_case in _DEGRADED_CASES
            and bool(self.omission_facts)
        )


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    execution: PlannedExecution
    disposition: ExecutionDisposition
    rca_success: bool
    knowledge: KnowledgeDisposition
    failure_case: FailureCase
    diagnostic_stage: str
    signal_started_at: str | None = None
    actionable_incident_observed_at: str | None = None
    usable_rca_observed_at: str | None = None
    latency_ms: int | None = None
    cutoff_ms: int | None = None
    detail: str = ""
    metric_disposition: ApprovedMetricDisposition | None = None

    def __post_init__(self) -> None:
        completed = self.disposition in {
            ExecutionDisposition.OBSERVED_COMPLETION,
            ExecutionDisposition.VALID_DEGRADED_COMPLETION,
        }
        if completed != (self.latency_ms is not None):
            raise ValueError("only observed completions have latency")
        if self.rca_success and not completed:
            raise ValueError("non-completion cannot be an RCA success")
        if self.disposition is ExecutionDisposition.RIGHT_CENSORED_UNFINISHED and self.cutoff_ms is None:
            raise ValueError("right-censored result requires a cutoff")
        if self.disposition is ExecutionDisposition.TERMINAL_PRODUCTION_FAILURE:
            inferred = (
                ApprovedMetricDisposition.INVALID_OUTPUT
                if self.failure_case is FailureCase.SCHEMA_OR_VALIDATION_FAILURE
                else ApprovedMetricDisposition.SYSTEM_FAILURE
            )
        elif self.disposition is ExecutionDisposition.RIGHT_CENSORED_UNFINISHED:
            inferred = ApprovedMetricDisposition.TIMEOUT
        elif self.disposition is ExecutionDisposition.INVALID_EVALUATION:
            inferred = {
                "EXTERNAL_OBSERVER": ApprovedMetricDisposition.INVALID_OBSERVATION,
                "ISOLATION_BREACH": ApprovedMetricDisposition.ISOLATION_BREACH,
            }.get(self.diagnostic_stage, ApprovedMetricDisposition.INVALID_EVALUATION)
        else:
            inferred = (ApprovedMetricDisposition.PASS if self.rca_success
                        else ApprovedMetricDisposition.RCA_FAIL)
        if self.metric_disposition is None:
            object.__setattr__(self, "metric_disposition", inferred)
        elif not isinstance(self.metric_disposition, ApprovedMetricDisposition):
            raise TypeError("metric_disposition must use the Approved §8.4 vocabulary")
        elif self.metric_disposition is not inferred:
            raise ValueError(
                "requested metric disposition contradicts the authoritative observed outcome"
            )
