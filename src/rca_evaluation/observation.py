"""Fail-closed traversal of production public semantic lineage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from incident_management.contracts import IncidentStatus
from knowledge_index.contracts import (
    KnowledgeReadStatus,
    KnowledgeSnapshotKey,
)
from llm_generation.contracts import LocalReadStatus
from rca_persistence.contracts import LogicalTryResultKind

from .identity import commitment, require_identity
from .ports import (
    EvidencePublicReads,
    GenerationPublicReads,
    IncidentPublicReads,
    KnowledgePublicReads,
    RcaPublicReads,
)


class ObservationOutcome(str, Enum):
    RESOLVED = "RESOLVED"
    INVALID_OBSERVATION = "INVALID_OBSERVATION"
    SYSTEM_FAILURE = "SYSTEM_FAILURE"


class ObservationDetail(str, Enum):
    COMPLETE = "COMPLETE"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    CORRUPT = "CORRUPT"
    CONTRADICTORY = "CONTRADICTORY"


@dataclass(frozen=True, slots=True)
class CapturedInputBoundary:
    """Evaluation-side capture of the exact new Event submitted for an execution."""

    execution_id: str
    event_id: str
    event_content_commitment: str

    def __post_init__(self) -> None:
        require_identity(self.execution_id, "execution")
        for name in ("event_id", "event_content_commitment"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty trimmed value")
        if not self.event_content_commitment.startswith("sha256:"):
            raise ValueError("event_content_commitment must be a SHA-256 commitment")


@dataclass(frozen=True, slots=True)
class PublicObservation:
    execution_id: str
    event_id: str
    incident_id: str
    aggregate_id: str
    current_version_id: str
    attempt_id: str
    try_ordinal: int
    validated_result_id: str
    evidence_snapshot_id: str
    evidence_revision_id: str
    evidence_commitment: str
    knowledge_snapshot_id: str
    knowledge_commitment: str
    lineage_commitment: str


@dataclass(frozen=True, slots=True)
class ObservationResolution:
    outcome: ObservationOutcome
    detail: ObservationDetail
    reason: str
    observation: PublicObservation | None = None

    def __post_init__(self) -> None:
        if self.outcome is ObservationOutcome.RESOLVED:
            if self.detail is not ObservationDetail.COMPLETE or self.observation is None:
                raise ValueError("resolved observation requires complete public lineage")
        elif self.observation is not None or self.detail is ObservationDetail.COMPLETE:
            raise ValueError("failed observation cannot carry a public observation")


class _Missing(ValueError):
    pass


class _Contradictory(ValueError):
    pass


class _Corrupt(ValueError):
    pass


class PublicObservationResolver:
    """Uses only declared public reads; it owns no production persistence handle."""

    def __init__(self, incidents: IncidentPublicReads, rca: RcaPublicReads,
                 evidence: EvidencePublicReads, knowledge: KnowledgePublicReads,
                 generation: GenerationPublicReads) -> None:
        self._incidents = incidents
        self._rca = rca
        self._evidence = evidence
        self._knowledge = knowledge
        self._generation = generation

    def resolve(self, captured: CapturedInputBoundary) -> ObservationResolution:
        if not isinstance(captured, CapturedInputBoundary):
            raise TypeError("captured must be a CapturedInputBoundary")
        try:
            observation = self._resolve(captured)
            return ObservationResolution(
                ObservationOutcome.RESOLVED, ObservationDetail.COMPLETE,
                "authoritative public lineage resolved", observation,
            )
        except _Missing as exc:
            return ObservationResolution(
                ObservationOutcome.INVALID_OBSERVATION, ObservationDetail.MISSING, str(exc),
            )
        except _Contradictory as exc:
            return ObservationResolution(
                ObservationOutcome.INVALID_OBSERVATION, ObservationDetail.CONTRADICTORY, str(exc),
            )
        except _Corrupt as exc:
            return ObservationResolution(
                ObservationOutcome.INVALID_OBSERVATION, ObservationDetail.CORRUPT, str(exc),
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            return ObservationResolution(
                ObservationOutcome.SYSTEM_FAILURE, ObservationDetail.UNAVAILABLE,
                f"public read unavailable: {type(exc).__name__}",
            )
        except Exception as exc:
            disposition = getattr(getattr(exc, "retry_disposition", None), "value", None)
            if disposition == "RETRYABLE":
                return ObservationResolution(
                    ObservationOutcome.SYSTEM_FAILURE, ObservationDetail.UNAVAILABLE,
                    f"public read unavailable: {type(exc).__name__}",
                )
            return ObservationResolution(
                ObservationOutcome.INVALID_OBSERVATION, ObservationDetail.CORRUPT,
                f"public read failed closed: {type(exc).__name__}",
            )

    def _resolve(self, captured: CapturedInputBoundary) -> PublicObservation:
        event_id = captured.event_id
        has_owner = self._incidents.event_has_incident_owner(event_id)
        if type(has_owner) is not bool:
            raise _Corrupt("Event ownership read returned an invalid type")

        matches = []
        for status in IncidentStatus:
            records = self._incidents.list_incidents_by_workflow_status(status)
            if not isinstance(records, tuple):
                raise _Corrupt("Incident list read returned an invalid type")
            for record in records:
                if not hasattr(record, "event_ids") or not hasattr(record, "incident_id"):
                    raise _Corrupt("Incident list contains an invalid public record")
                if event_id in record.event_ids:
                    matches.append(record)
        if not has_owner:
            if matches:
                raise _Contradictory("Event ownership absence contradicts Incident public records")
            raise _Missing("Event has no authoritative Incident owner")
        if len(matches) != 1:
            raise _Contradictory("Event must resolve to exactly one authoritative Incident")

        incident_id = matches[0].incident_id
        incident = self._incidents.get_incident(incident_id)
        if incident is None:
            raise _Contradictory("Incident enumeration contradicts point read")
        if incident.incident_id != incident_id or event_id not in incident.event_ids:
            raise _Contradictory("Incident point read contradicts Event ownership")
        relationship = self._incidents.get_rca_relationship(incident_id)
        if relationship is None:
            raise _Contradictory("Incident RCA relationship is unavailable for an existing Incident")
        if relationship.incident_id != incident_id:
            raise _Contradictory("Incident RCA relationship identity mismatch")

        aggregate = self._rca.get_aggregate_by_incident(incident_id)
        if aggregate is None:
            if relationship.current_version_id is not None:
                raise _Contradictory("Incident Current reference has no RCA Aggregate")
            raise _Missing("Incident has no RCA Aggregate")
        if aggregate.incident_id != incident_id:
            raise _Contradictory("RCA Aggregate belongs to another Incident")
        current = self._rca.get_current(aggregate.aggregate_id)
        if current is None:
            if relationship.current_version_id is not None:
                raise _Contradictory("Incident Current reference contradicts RCA Current absence")
            raise _Missing("RCA Aggregate has no Current Version")
        version = current.version
        if (current.current.aggregate_id != aggregate.aggregate_id
                or version.aggregate_id != aggregate.aggregate_id
                or relationship.current_version_id != version.version_id):
            raise _Contradictory("Incident, Aggregate, and Current Version lineage disagree")
        history = self._rca.get_version_history(aggregate.aggregate_id)
        if not isinstance(history, tuple):
            raise _Corrupt("Version history read returned an invalid type")
        if sum(item.version_id == version.version_id for item in history) != 1:
            raise _Contradictory("Current Version does not resolve exactly once in history")

        provenance = self._rca.get_artifact_provenance(version.version_id)
        if provenance is None:
            raise _Missing("Current Version has no public Artifact provenance")
        lineage_read = self._rca.get_attempt_lineage(version.attempt_id)
        if lineage_read is None:
            raise _Missing("Current Version Attempt lineage is absent")
        lineage = lineage_read.attempt.lineage
        if lineage.attempt_id != version.attempt_id or lineage.aggregate_id != aggregate.aggregate_id:
            raise _Contradictory("Version and Attempt lineage disagree")
        if (provenance.evidence_snapshot_id != lineage.evidence_snapshot_id
                or provenance.evidence_revision_id != lineage.evidence_revision_id
                or provenance.knowledge_snapshot_id != lineage.knowledge_snapshot_id):
            raise _Contradictory("Artifact provenance and Attempt lineage disagree")

        snapshot = self._evidence.resolve_snapshot(lineage.evidence_snapshot_id)
        revision = self._evidence.resolve_revision(lineage.evidence_revision_id)
        if snapshot is None or revision is None:
            raise _Missing("Evidence Snapshot or Revision is absent")
        if (snapshot.snapshot_id != lineage.evidence_snapshot_id
                or snapshot.revision_id != lineage.evidence_revision_id
                or snapshot.incident_id != incident_id
                or revision.revision_id != lineage.evidence_revision_id
                or revision.incident_id != incident_id):
            raise _Contradictory("Evidence public lineage disagrees with the Attempt")
        snapshot_content = getattr(snapshot, "canonical_snapshot_content", None)
        revision_integrity = getattr(revision, "integrity_identity", None)
        if not isinstance(snapshot_content, str) or not isinstance(revision_integrity, str):
            raise _Corrupt("Evidence public commitment material is unavailable")
        evidence_commitment = commitment({
            "snapshot_id": snapshot.snapshot_id,
            "revision_id": revision.revision_id,
            "canonical_snapshot_content": snapshot_content,
            "revision_integrity_identity": revision_integrity,
        })

        knowledge_key = KnowledgeSnapshotKey(lineage.knowledge_snapshot_id)
        knowledge_snapshot = self._knowledge.read_snapshot(knowledge_key)
        knowledge_provenance = self._knowledge.read_provenance(knowledge_key)
        self._require_knowledge_found(knowledge_snapshot, "Knowledge Snapshot")
        self._require_knowledge_found(knowledge_provenance, "Knowledge provenance")
        if getattr(knowledge_snapshot.value, "snapshot_key", None) != knowledge_key:
            raise _Contradictory("Knowledge Snapshot identity disagrees with Attempt lineage")
        if getattr(knowledge_provenance.value, "snapshot_identity", None) != knowledge_key:
            raise _Contradictory("Knowledge provenance identity disagrees with Attempt lineage")
        snapshot_commitment = getattr(knowledge_snapshot.value, "snapshot_commitment", None)
        provenance_commitment = getattr(knowledge_provenance.value, "snapshot_commitment", None)
        if (not isinstance(snapshot_commitment, str) or not snapshot_commitment
                or snapshot_commitment != provenance_commitment):
            raise _Contradictory("Knowledge Snapshot commitment and provenance disagree")

        validated = tuple(
            outcome for outcome in lineage_read.try_outcomes
            if outcome.result_kind is LogicalTryResultKind.VALIDATED_RESULT
        )
        if len(validated) != 1 or validated[0].validated_result_id is None:
            raise _Contradictory("Attempt must resolve to exactly one validated generation result")
        outcome = validated[0]
        generation_read = self._generation.result(outcome.validated_result_id)
        status = getattr(generation_read, "status", None)
        if status is LocalReadStatus.UNAVAILABLE:
            raise OSError("generation result read unavailable")
        if status is LocalReadStatus.NOT_FOUND:
            raise _Missing("validated generation result is absent")
        if status in {LocalReadStatus.INVALID, LocalReadStatus.REPAIR_REQUIRED}:
            raise _Corrupt("validated generation result read is invalid")
        result = getattr(generation_read, "value", None)
        if status is not LocalReadStatus.FOUND or result is None:
            raise _Corrupt("generation result read returned an invalid public result")
        if result.validated_result_id != outcome.validated_result_id:
            raise _Contradictory("generation result identity disagrees with Try outcome")
        content_input = result.content.input
        if (content_input.try_identity != outcome.identity
                or content_input.evidence_snapshot_id != lineage.evidence_snapshot_id
                or content_input.evidence_revision_id != lineage.evidence_revision_id
                or content_input.knowledge_snapshot_id != lineage.knowledge_snapshot_id):
            raise _Contradictory("generation result input disagrees with public lineage")

        facts = {
            "event_id": event_id,
            "incident_id": incident_id,
            "aggregate_id": aggregate.aggregate_id,
            "current_version_id": version.version_id,
            "attempt_id": lineage.attempt_id,
            "try_ordinal": outcome.identity.try_ordinal,
            "validated_result_id": outcome.validated_result_id,
            "evidence_snapshot_id": lineage.evidence_snapshot_id,
            "evidence_revision_id": lineage.evidence_revision_id,
            "evidence_commitment": evidence_commitment,
            "knowledge_snapshot_id": lineage.knowledge_snapshot_id,
            "knowledge_commitment": snapshot_commitment,
        }
        return PublicObservation(
            captured.execution_id, event_id, incident_id, aggregate.aggregate_id,
            version.version_id, lineage.attempt_id, outcome.identity.try_ordinal,
            outcome.validated_result_id, lineage.evidence_snapshot_id,
            lineage.evidence_revision_id, evidence_commitment,
            lineage.knowledge_snapshot_id, snapshot_commitment,
            commitment(facts),
        )

    @staticmethod
    def _require_knowledge_found(read: object, label: str) -> None:
        status = getattr(read, "status", None)
        if status is KnowledgeReadStatus.UNAVAILABLE:
            raise OSError(f"{label} unavailable")
        if status is KnowledgeReadStatus.NOT_FOUND:
            raise _Missing(f"{label} is absent")
        if status in {KnowledgeReadStatus.INVALID, KnowledgeReadStatus.REPAIR_REQUIRED}:
            raise _Corrupt(f"{label} is invalid")
        if status is not KnowledgeReadStatus.FOUND or getattr(read, "value", None) is None:
            raise _Corrupt(f"{label} read returned an invalid public result")
