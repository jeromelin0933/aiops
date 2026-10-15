"""Typed conclusion and Knowledge facts used by authority-driven scoring."""

from __future__ import annotations

from dataclasses import dataclass

from knowledge_index.contracts import RetrievalResolution
from rca_persistence.contracts import DiagnosticConclusion, EvidenceCompleteness


@dataclass(frozen=True, slots=True)
class ConclusionFacts:
    conclusion: DiagnosticConclusion
    operational_cause_grounded: bool
    complete_causal_chain: bool
    leading_hypothesis: bool
    major_contradiction: bool
    competing_alternatives_unresolved: bool

    def __post_init__(self) -> None:
        if not isinstance(self.conclusion, DiagnosticConclusion):
            raise TypeError("conclusion must be the production typed value")
        for field in (
            "operational_cause_grounded", "complete_causal_chain",
            "leading_hypothesis", "major_contradiction",
            "competing_alternatives_unresolved",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError("conclusion facts must be booleans")


def conclusion_calibration_valid(
    value: ConclusionFacts, *, evidence_completeness: EvidenceCompleteness,
    required_evidence_satisfied: bool, granularity_valid: bool,
    allowed_degraded_conclusions: tuple[str, ...],
) -> bool:
    """Derive calibration; callers cannot assert its result."""
    if not isinstance(value, ConclusionFacts):
        raise TypeError("typed conclusion facts are required")
    if not isinstance(evidence_completeness, EvidenceCompleteness):
        raise TypeError("Evidence completeness must be closed")
    if type(required_evidence_satisfied) is not bool or type(granularity_valid) is not bool:
        raise TypeError("derived calibration predicates must be booleans")
    if (evidence_completeness is EvidenceCompleteness.DEGRADED
            and value.conclusion.value not in allowed_degraded_conclusions):
        return False
    if value.conclusion is DiagnosticConclusion.IDENTIFIED:
        return (
            required_evidence_satisfied and granularity_valid
            and value.operational_cause_grounded and value.complete_causal_chain
            and not value.major_contradiction
            and not value.competing_alternatives_unresolved
        )
    if value.conclusion is DiagnosticConclusion.MOST_SUPPORTED:
        return (
            value.leading_hypothesis and not value.major_contradiction
            and (not value.complete_causal_chain
                 or not required_evidence_satisfied
                 or value.competing_alternatives_unresolved)
        )
    if value.conclusion is DiagnosticConclusion.INCONCLUSIVE:
        return (
            not required_evidence_satisfied
            or not value.operational_cause_grounded
            or value.competing_alternatives_unresolved
            or value.major_contradiction
        )
    return False


@dataclass(frozen=True, slots=True)
class KnowledgeBoundaryInput:
    resolution: RetrievalResolution
    knowledge_gap: bool
    fabricated_reference: bool
    sop_backed_guidance: bool
    incident_truth_depends_on_knowledge: bool
    unavailable_reported_as_no_match: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.resolution, RetrievalResolution):
            raise TypeError("Knowledge resolution must be a closed public value")
        for field in (
            "knowledge_gap", "fabricated_reference", "sop_backed_guidance",
            "incident_truth_depends_on_knowledge", "unavailable_reported_as_no_match",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError("Knowledge boundary facts must be booleans")


def knowledge_boundary_valid(value: KnowledgeBoundaryInput) -> bool:
    if (value.fabricated_reference or value.incident_truth_depends_on_knowledge
            or value.unavailable_reported_as_no_match):
        return False
    if value.resolution is RetrievalResolution.MATCH:
        return True
    if value.resolution is RetrievalResolution.NO_MATCH:
        return value.knowledge_gap and not value.sop_backed_guidance
    if value.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE:
        return value.knowledge_gap and not value.sop_backed_guidance
    return False
