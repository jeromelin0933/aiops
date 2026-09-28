"""Pure, lossless projection of admitted Candidate-D analysis into Candidate A."""

from __future__ import annotations

from dataclasses import asdict

from rca_persistence.contracts import (
    ArtifactClaim,
    ArtifactClaimSemantics,
    ArtifactCausalAssertion,
    ArtifactCausalSupportProof,
    ArtifactEvidenceFact,
    ArtifactKnowledgeFact,
    ArtifactProvenance,
    GenerationProvenance,
    RcaAction,
    RcaArtifact,
    RcaHypothesis,
)

from .contracts import ValidatedGenerationResult


def project_artifact(result: ValidatedGenerationResult) -> RcaArtifact:
    """Construct exactly one A-side semantic value; do not commit or publish it."""

    if not isinstance(result, ValidatedGenerationResult):
        raise TypeError("an immutable validated result is required")
    content = result.content
    source = content.input
    claims = ArtifactClaimSemantics(
        content.contract_version,
        tuple(ArtifactClaim(
            item.claim_id, item.category, item.text,
            item.supporting_evidence_ids, item.contradicting_evidence_ids,
            item.knowledge_reference_ids, item.evidential_support,
        ) for item in content.claims),
        tuple(ArtifactEvidenceFact(**asdict(item)) for item in content.evidence_references),
        tuple(ArtifactKnowledgeFact(**asdict(item)) for item in content.knowledge_references),
        content.summary_claim_ids,
        content.severity_claim_ids,
        tuple(item.claim_ids for item in content.hypotheses),
        tuple(item.claim_ids for item in content.remediation),
        tuple(item.claim_ids for item in content.prevention),
        tuple(ArtifactCausalAssertion(
            item.claim_id, item.causal_assertion.cause_claim_ids,
            item.causal_assertion.effect_claim_ids, item.causal_assertion.relation.value,
        ) for item in content.claims if item.causal_assertion is not None),
        tuple(ArtifactCausalSupportProof(**asdict(item)) for item in content.causal_proofs),
    )
    provenance = ArtifactProvenance(
        source.evidence_snapshot_id,
        source.evidence_revision_id,
        source.knowledge_snapshot_id,
        (),
        (),
        GenerationProvenance(
            source.pin.provider,
            source.pin.model,
            source.pin.prompt.identity,
            source.pin.configuration.identity,
            source.pin.profile.identity,
        ),
    )
    return RcaArtifact(
        content.summary,
        content.severity_assessment,
        content.diagnostic_conclusion,
        tuple(
            RcaHypothesis(
                item.rank, item.statement, item.evidential_support,
                item.supporting_evidence_ids, item.contradicting_evidence_ids,
                item.knowledge_reference_ids, item.reasoning_summary,
            )
            for item in content.hypotheses
        ),
        tuple(RcaAction(item.description, item.source, item.knowledge_reference_ids) for item in content.remediation),
        tuple(RcaAction(item.description, item.source, item.knowledge_reference_ids) for item in content.prevention),
        content.limitations,
        content.evidence_completeness,
        content.knowledge_gap,
        provenance,
        claims,
    )
