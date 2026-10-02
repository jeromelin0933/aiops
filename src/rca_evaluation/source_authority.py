"""S3 authority over the exact S2 observation and SPEC-015 generation result."""

from __future__ import annotations

from dataclasses import dataclass

from knowledge_index.contracts import (
    KnowledgeReadStatus, KnowledgeSnapshotKey, RetrievalResolution,
)
from llm_generation.contracts import LocalReadStatus, ValidatedGenerationResult
from rca_persistence.contracts import (
    DiagnosticConclusion, EvidenceCompleteness, EvidentialSupport, GuidanceSource,
)

from .identity import commitment, require_identity
from .observation import (
    CapturedInputBoundary, ObservationOutcome, PublicObservationResolver,
)
from .ports import GenerationPublicReads, KnowledgePublicReads
from .claims import production_claim_commitment


@dataclass(frozen=True, slots=True)
class LineagedBooleanFact:
    value: bool
    source_identity: str
    source_commitment: str
    derivation: str

    def __post_init__(self) -> None:
        if type(self.value) is not bool:
            raise TypeError("lineaged fact value must be boolean")
        for value in (self.source_identity, self.source_commitment, self.derivation):
            if not isinstance(value, str) or not value:
                raise ValueError("lineaged fact requires source and derivation")

    def material(self) -> dict:
        return {
            "value": self.value, "source_identity": self.source_identity,
            "source_commitment": self.source_commitment, "derivation": self.derivation,
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeProductionFacts:
    execution_id: str
    observation_lineage_commitment: str
    validated_result_id: str
    production_output_commitment: str
    typed_claims_commitment: str
    evidence_snapshot_id: str
    evidence_revision_id: str
    evidence_commitment: str
    knowledge_snapshot_id: str
    knowledge_commitment: str
    knowledge_resolution: RetrievalResolution
    diagnostic_conclusion: DiagnosticConclusion
    evidence_completeness: EvidenceCompleteness
    knowledge_gap: bool
    leading_hypothesis_fact: LineagedBooleanFact
    major_contradiction_fact: LineagedBooleanFact
    competing_alternatives_fact: LineagedBooleanFact
    sop_backed_guidance_fact: LineagedBooleanFact
    production_projection: dict

    def __post_init__(self) -> None:
        require_identity(self.execution_id, "execution")
        if (not isinstance(self.knowledge_resolution, RetrievalResolution)
                or not isinstance(self.diagnostic_conclusion, DiagnosticConclusion)
                or not isinstance(self.evidence_completeness, EvidenceCompleteness)
                or type(self.knowledge_gap) is not bool):
            raise TypeError("authoritative production facts require closed public values")
        for field in (
            "leading_hypothesis_fact", "major_contradiction_fact",
            "competing_alternatives_fact", "sop_backed_guidance_fact",
        ):
            if not isinstance(getattr(self, field), LineagedBooleanFact):
                raise TypeError("machine-derived calibration facts require lineage")

    @property
    def machine_leading_hypothesis(self) -> bool:
        return self.leading_hypothesis_fact.value

    @property
    def machine_major_contradiction(self) -> bool:
        return self.major_contradiction_fact.value

    @property
    def machine_competing_alternatives(self) -> bool:
        return self.competing_alternatives_fact.value

    @property
    def machine_sop_backed_guidance(self) -> bool:
        return self.sop_backed_guidance_fact.value

    def material(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "observation_lineage_commitment": self.observation_lineage_commitment,
            "validated_result_id": self.validated_result_id,
            "production_output_commitment": self.production_output_commitment,
            "typed_claims_commitment": self.typed_claims_commitment,
            "evidence_snapshot_id": self.evidence_snapshot_id,
            "evidence_revision_id": self.evidence_revision_id,
            "evidence_commitment": self.evidence_commitment,
            "knowledge_snapshot_id": self.knowledge_snapshot_id,
            "knowledge_commitment": self.knowledge_commitment,
            "knowledge_resolution": self.knowledge_resolution.value,
            "diagnostic_conclusion": self.diagnostic_conclusion.value,
            "evidence_completeness": self.evidence_completeness.value,
            "knowledge_gap": self.knowledge_gap,
            "machine_facts": {
                "leading_hypothesis": self.leading_hypothesis_fact.material(),
                "major_contradiction": self.major_contradiction_fact.material(),
                "competing_alternatives": self.competing_alternatives_fact.material(),
                "sop_backed_guidance": self.sop_backed_guidance_fact.material(),
            },
            "production_projection": self.production_projection,
        }

    @property
    def source_commitment(self) -> str:
        return commitment(self.material())


class S3SourceAuthority:
    """Resolve S3 inputs through S2 public lineage and exact public read ports."""

    def __init__(self, observations: PublicObservationResolver,
                 generation: GenerationPublicReads, knowledge: KnowledgePublicReads) -> None:
        if type(observations) is not PublicObservationResolver:
            raise TypeError("S2 PublicObservationResolver authority is required")
        if not callable(getattr(generation, "result", None)):
            raise TypeError("SPEC-015 public generation reads are required")
        if not callable(getattr(knowledge, "read_snapshot", None)):
            raise TypeError("Knowledge public reads are required")
        self._observations = observations
        self._generation = generation
        self._knowledge = knowledge

    def resolve(self, captured: CapturedInputBoundary) -> AuthoritativeProductionFacts:
        resolution = self._observations.resolve(captured)
        if resolution.outcome is not ObservationOutcome.RESOLVED or resolution.observation is None:
            raise ValueError("S2 authoritative production observation is not uniquely resolved")
        observed = resolution.observation
        if observed.execution_id != captured.execution_id:
            raise ValueError("S2 observation belongs to another execution")

        result_read = self._generation.result(observed.validated_result_id)
        if getattr(result_read, "status", None) is not LocalReadStatus.FOUND:
            raise ValueError("exact SPEC-015 generation result is unavailable")
        result = getattr(result_read, "value", None)
        if type(result) is not ValidatedGenerationResult:
            raise ValueError("generation public read did not return the exact typed result")
        if result.validated_result_id != observed.validated_result_id:
            raise ValueError("generation result identity contradicts S2 lineage")
        source = result.content.input
        if (source.evidence_snapshot_id != observed.evidence_snapshot_id
                or source.evidence_revision_id != observed.evidence_revision_id
                or source.knowledge_snapshot_id != observed.knowledge_snapshot_id):
            raise ValueError("generation result provenance contradicts S2 lineage")

        knowledge_read = self._knowledge.read_snapshot(
            KnowledgeSnapshotKey(observed.knowledge_snapshot_id),
        )
        if getattr(knowledge_read, "status", None) is not KnowledgeReadStatus.FOUND:
            raise ValueError("exact Knowledge Snapshot is unavailable")
        snapshot = getattr(knowledge_read, "value", None)
        if (getattr(getattr(snapshot, "snapshot_key", None), "value", None)
                != observed.knowledge_snapshot_id
                or getattr(snapshot, "snapshot_commitment", None) != observed.knowledge_commitment
                or getattr(snapshot, "resolution", None) is not source.knowledge_resolution
                or getattr(snapshot, "knowledge_gap", None) is not result.content.knowledge_gap):
            raise ValueError("Knowledge Snapshot facts contradict generation or S2 lineage")

        claims_material = [production_claim_commitment(item) for item in result.content.claims]
        typed_claims_commitment = commitment(claims_material)
        top = result.content.hypotheses[0]
        machine_leading = (
            top.rank == 1
            and top.evidential_support in {EvidentialSupport.HIGH, EvidentialSupport.MEDIUM}
            and bool(top.supporting_evidence_ids)
        )
        machine_contradiction = bool(top.contradicting_evidence_ids)
        machine_competing = any(
            item.evidential_support in {EvidentialSupport.HIGH, EvidentialSupport.MEDIUM}
            and not item.contradicting_evidence_ids
            for item in result.content.hypotheses[1:]
        )
        machine_sop_guidance = any(
            item.source is GuidanceSource.SOP_BACKED
            for item in (*result.content.remediation, *result.content.prevention)
        )
        projection = {
            "summary": result.content.summary,
            "severity_assessment": result.content.severity_assessment,
            "diagnostic_conclusion": result.content.diagnostic_conclusion.value,
            "evidence_completeness": result.content.evidence_completeness.value,
            "knowledge_gap": result.content.knowledge_gap,
            "claims": [{
                "claim_id": item.claim_id,
                "category": item.category.value,
                "text": item.text,
                "supporting_evidence_ids": list(item.supporting_evidence_ids),
                "contradicting_evidence_ids": list(item.contradicting_evidence_ids),
                "knowledge_reference_ids": list(item.knowledge_reference_ids),
                "commitment": production_claim_commitment(item),
            } for item in result.content.claims],
            "hypotheses": [{
                "rank": item.rank, "statement": item.statement,
                "evidential_support": item.evidential_support.value,
                "supporting_evidence_ids": list(item.supporting_evidence_ids),
                "contradicting_evidence_ids": list(item.contradicting_evidence_ids),
                "claim_ids": list(item.claim_ids),
            } for item in result.content.hypotheses],
        }
        return AuthoritativeProductionFacts(
            captured.execution_id, observed.lineage_commitment,
            result.validated_result_id, result.semantic_commitment,
            typed_claims_commitment, observed.evidence_snapshot_id,
            observed.evidence_revision_id, observed.evidence_commitment,
            observed.knowledge_snapshot_id, observed.knowledge_commitment,
            source.knowledge_resolution, result.content.diagnostic_conclusion,
            result.content.evidence_completeness, result.content.knowledge_gap,
            LineagedBooleanFact(
                machine_leading, result.validated_result_id, result.semantic_commitment,
                "SPEC015_TOP_HYPOTHESIS_RANK_SUPPORT_AND_EVIDENCE",
            ),
            LineagedBooleanFact(
                machine_contradiction, result.validated_result_id, result.semantic_commitment,
                "SPEC015_TOP_HYPOTHESIS_CONTRADICTING_EVIDENCE",
            ),
            LineagedBooleanFact(
                machine_competing, result.validated_result_id, result.semantic_commitment,
                "SPEC015_LOWER_HYPOTHESIS_SUPPORT_WITHOUT_CONTRADICTION",
            ),
            LineagedBooleanFact(
                machine_sop_guidance, result.validated_result_id, result.semantic_commitment,
                "SPEC015_TYPED_GUIDANCE_SOURCE",
            ),
            projection,
        )
