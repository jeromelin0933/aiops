"""Read-only public-authority checks before projecting a durable D result."""

from __future__ import annotations

from dataclasses import dataclass

from incident_evidence.contracts import EvidenceRevision, EvidenceSnapshot
from knowledge_index.contracts import (
    KnowledgeProvenanceProjection, KnowledgeReadStatus,
    KnowledgeSnapshotEnvelope, KnowledgeSnapshotKey,
)
from rca_persistence.contracts import (
    AttemptLineageRead, LogicalTryResultKind, RcaArtifact,
)

from .config import GenerationConfigError, load_generation_config
from .contracts import LocalReadStatus, ValidatedGenerationResult
from .ports import AttemptPublicReader, EvidencePublicReader, KnowledgePublicReader
from .projection import project_artifact
from .service import canonical_evidence_projection, canonical_knowledge_projection
from .sqlite_store import CandidateDStore


@dataclass(frozen=True, slots=True)
class HandoffProjection:
    status: LocalReadStatus
    result: ValidatedGenerationResult | None = None
    artifact: RcaArtifact | None = None


class CandidateDHandoffFacade:
    """Resolve D truth and fresh public A/B/C facts; never mutate them."""

    def __init__(
        self, store: CandidateDStore, attempts: AttemptPublicReader,
        evidence: EvidencePublicReader, knowledge: KnowledgePublicReader,
    ) -> None:
        self.store = store
        self.attempts = attempts
        self.evidence = evidence
        self.knowledge = knowledge

    def resolve(self, validated_result_id: str) -> HandoffProjection:
        readiness = self.store.local_readiness()
        if readiness is not LocalReadStatus.FOUND:
            return HandoffProjection(readiness)
        read = self.store.result(validated_result_id)
        if read.status is not LocalReadStatus.FOUND:
            return HandoffProjection(read.status)
        result = read.value
        source = result.content.input
        try:
            config = load_generation_config(self.store.resource_config_path)
            if (config.pin != source.pin
                or result.content.contract_version != source.pin.result_schema.version):
                return HandoffProjection(LocalReadStatus.REPAIR_REQUIRED)
            view = self.attempts.get_attempt_lineage(source.try_identity.attempt_id)
            if not isinstance(view, AttemptLineageRead):
                return HandoffProjection(LocalReadStatus.REPAIR_REQUIRED)
            lineage = view.attempt.lineage
            if (lineage.attempt_id != source.try_identity.attempt_id
                or lineage.evidence_snapshot_id != source.evidence_snapshot_id
                or lineage.evidence_revision_id != source.evidence_revision_id
                or lineage.knowledge_snapshot_id != source.knowledge_snapshot_id
                or not source.pin.matches_attempt(lineage.generation_provenance)):
                return HandoffProjection(LocalReadStatus.REPAIR_REQUIRED)
            if source.try_identity.try_ordinal > len(view.try_outcomes) + 1:
                return HandoffProjection(LocalReadStatus.REPAIR_REQUIRED)
            for outcome in view.try_outcomes:
                if outcome.identity == source.try_identity and (
                    outcome.result_kind is not LogicalTryResultKind.VALIDATED_RESULT
                    or outcome.validated_result_id != validated_result_id
                ):
                    return HandoffProjection(LocalReadStatus.REPAIR_REQUIRED)
            snapshot = self.evidence.resolve_snapshot(source.evidence_snapshot_id)
            revision = self.evidence.resolve_revision(source.evidence_revision_id)
            if (not isinstance(snapshot, EvidenceSnapshot)
                or not isinstance(revision, EvidenceRevision)
                or snapshot.snapshot_id != source.evidence_snapshot_id
                or snapshot.revision_id != source.evidence_revision_id
                or revision.revision_id != source.evidence_revision_id
                or snapshot.incident_id != revision.incident_id
                or snapshot.snapshot_content["semantic_evidence"] != revision.semantic_content
                or source.evidence_projection != canonical_evidence_projection(snapshot)):
                return HandoffProjection(LocalReadStatus.REPAIR_REQUIRED)
            key = KnowledgeSnapshotKey(source.knowledge_snapshot_id)
            knowledge = self.knowledge.read_snapshot(key)
            provenance = self.knowledge.read_provenance(key)
            if (knowledge.status is not KnowledgeReadStatus.FOUND
                or provenance.status is not KnowledgeReadStatus.FOUND
                or not isinstance(knowledge.value, KnowledgeSnapshotEnvelope)
                or not isinstance(provenance.value, KnowledgeProvenanceProjection)
                or knowledge.value.snapshot_key != key
                or knowledge.value.resolution is not source.knowledge_resolution
                or provenance.value.resolution is not knowledge.value.resolution
                or source.knowledge_projection != canonical_knowledge_projection(
                    knowledge.value, provenance.value,
                )):
                return HandoffProjection(LocalReadStatus.REPAIR_REQUIRED)
            return HandoffProjection(LocalReadStatus.FOUND, result, project_artifact(result))
        except GenerationConfigError:
            return HandoffProjection(LocalReadStatus.REPAIR_REQUIRED)
        except (ValueError, TypeError, KeyError):
            return HandoffProjection(LocalReadStatus.REPAIR_REQUIRED)
        except Exception:
            return HandoffProjection(LocalReadStatus.UNAVAILABLE)
