"""Rules-first Root Cause Identification against an exact admitted Oracle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from rca_persistence.contracts import EvidenceCompleteness

from .calibration import (
    ConclusionFacts,
    KnowledgeBoundaryInput,
    conclusion_calibration_valid,
    knowledge_boundary_valid,
)
from .claims import ClaimEvaluationStatus
from .identity import commitment, require_identity
from .oracle import OracleRevision


class CausalClass(str, Enum):
    CREDENTIAL_ABUSE_BRUTE_FORCE = "CREDENTIAL_ABUSE_BRUTE_FORCE"
    DATABASE_LATENCY_CASCADE = "DATABASE_LATENCY_CASCADE"
    MEMORY_EXHAUSTION_SERVICE_FAILURE = "MEMORY_EXHAUSTION_SERVICE_FAILURE"
    EXTERNAL_DEPENDENCY_FAILURE = "EXTERNAL_DEPENDENCY_FAILURE"
    SHARED_DATABASE_CONNECTIVITY_CASCADE = "SHARED_DATABASE_CONNECTIVITY_CASCADE"
    REQUEST_SURGE_RATE_LIMIT = "REQUEST_SURGE_RATE_LIMIT"
    OTHER = "OTHER"


class ExecutionScorability(str, Enum):
    SCORABLE = "SCORABLE"
    NOT_EVALUABLE = "NOT_EVALUABLE"
    INVALID_EXECUTION = "INVALID_EXECUTION"
    INVALID_OUTPUT = "INVALID_OUTPUT"


class OverclaimFinding(str, Enum):
    NONE = "NONE"
    NON_MATERIAL = "NON_MATERIAL"
    MATERIAL = "MATERIAL"


@dataclass(frozen=True, slots=True)
class OracleScenarioCriteria:
    oracle_revision_id: str
    oracle_content_commitment: str
    scenario_id: str
    scenario_commitment: str
    expected_causal_class: CausalClass
    accepted_alternatives: tuple[str, ...]
    accepted_granularities: tuple[str, ...]
    required_full_evidence: tuple[str, ...]
    required_degraded_evidence: tuple[str, ...]
    allowed_degraded_conclusions: tuple[str, ...]
    material_forbidden_overclaims: tuple[str, ...]
    top_hypothesis_required: bool

    @classmethod
    def from_revision(cls, revision: OracleRevision, scenario_id: str) -> "OracleScenarioCriteria":
        revision.validate()
        if scenario_id not in {f"S{i}" for i in range(1, 7)}:
            raise ValueError("scenario must be S1 through S6")
        scenario = json.loads(revision.semantic_json)["scenarios"][scenario_id]
        try:
            expected = CausalClass(scenario["O1"]["causal_class"])
            granularities = tuple(scenario["O2"]["accepted_granularities"])
            alternatives = tuple(scenario["O3"]["accepted_alternatives"])
            forbidden = tuple(scenario["O4"]["material_forbidden_overclaims"])
            full = tuple(scenario["O5"]["required_evidence_classes"])
            degraded = tuple(scenario["O7"]["required_evidence_classes"])
            conclusions = tuple(scenario["O7"]["allowed_conclusions"])
            top_required = scenario["O10"].get("top_hypothesis_required")
            if top_required is not True:
                raise ValueError("O10 must require the top hypothesis")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Oracle scenario scoring fields are not machine-admissible") from exc
        collections = (granularities, alternatives, forbidden, full, degraded, conclusions)
        if (not granularities or not full
                or any(any(not isinstance(item, str) or not item for item in values)
                       for values in collections)):
            raise ValueError("Oracle scenario scoring collections are invalid")
        return cls(
            revision.oracle_revision_id, revision.semantic_content_commitment,
            scenario_id, commitment(scenario), expected, alternatives,
            granularities, full, degraded, conclusions, forbidden,
            top_required,
        )


@dataclass(frozen=True, slots=True)
class TopHypothesis:
    causal_class: CausalClass
    granularity: str
    accepted_alternative_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.causal_class, CausalClass):
            raise TypeError("top hypothesis causal class must be closed")
        if not isinstance(self.granularity, str) or not self.granularity.strip():
            raise ValueError("top hypothesis granularity is required")
        if (self.accepted_alternative_id is not None
                and (not isinstance(self.accepted_alternative_id, str)
                     or not self.accepted_alternative_id.strip())):
            raise ValueError("accepted alternative identity is invalid")


@dataclass(frozen=True, slots=True)
class RootCauseEvaluation:
    canonical_causal_class_match: bool
    approved_accepted_alternative_match: bool
    top_hypothesis_accepted: bool
    granularity_valid: bool
    required_evidence_satisfied: bool
    forbidden_overclaim_detected: OverclaimFinding
    conclusion_calibration_valid: bool
    knowledge_boundary_valid: bool
    root_cause_identification_success: bool
    disposition: ExecutionScorability


def evaluate_root_cause(
    *, criteria: OracleScenarioCriteria, top_hypothesis: TopHypothesis,
    lower_ranked_causal_classes: tuple[CausalClass, ...],
    observed_evidence_classes: tuple[str, ...],
    detected_overclaim_ids: tuple[str, ...],
    evidence_completeness: EvidenceCompleteness,
    conclusion_facts: ConclusionFacts,
    knowledge_facts: KnowledgeBoundaryInput,
    claim_status: ClaimEvaluationStatus,
    scorability: ExecutionScorability = ExecutionScorability.SCORABLE,
) -> RootCauseEvaluation:
    del lower_ranked_causal_classes  # Lower ranks never satisfy the top-hypothesis predicate.
    require_identity(criteria.oracle_revision_id, "oracle_revision")
    canonical = top_hypothesis.causal_class is criteria.expected_causal_class
    alternative = (
        top_hypothesis.accepted_alternative_id is not None
        and top_hypothesis.accepted_alternative_id in criteria.accepted_alternatives
    )
    top_accepted = canonical or alternative
    granularity = top_hypothesis.granularity in criteria.accepted_granularities
    required = (
        criteria.required_full_evidence
        if evidence_completeness is EvidenceCompleteness.FULL
        else criteria.required_degraded_evidence
    )
    evidence = set(required) <= set(observed_evidence_classes)
    material = bool(set(detected_overclaim_ids) & set(criteria.material_forbidden_overclaims))
    overclaim = OverclaimFinding.MATERIAL if material else (
        OverclaimFinding.NON_MATERIAL if detected_overclaim_ids else OverclaimFinding.NONE
    )
    effective = scorability
    if claim_status is ClaimEvaluationStatus.INVALID_OUTPUT:
        effective = ExecutionScorability.INVALID_OUTPUT
    calibration = conclusion_calibration_valid(
        conclusion_facts, evidence_completeness=evidence_completeness,
        required_evidence_satisfied=evidence, granularity_valid=granularity,
        allowed_degraded_conclusions=criteria.allowed_degraded_conclusions,
    )
    knowledge = knowledge_boundary_valid(knowledge_facts)
    success = (
        effective is ExecutionScorability.SCORABLE
        and criteria.top_hypothesis_required and top_accepted and granularity and evidence
        and overclaim is not OverclaimFinding.MATERIAL
        and calibration and knowledge
    )
    return RootCauseEvaluation(
        canonical, alternative, top_accepted, granularity, evidence, overclaim,
        calibration, knowledge, success, effective,
    )
