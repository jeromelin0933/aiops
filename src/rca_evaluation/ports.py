"""Consumer-owned read-only ports over existing production public contracts."""

from __future__ import annotations

from typing import Protocol

from incident_evidence.contracts import EvidenceRevision, EvidenceSnapshot
from incident_management.contracts import (
    IncidentRcaRelationship,
    IncidentRecord,
    IncidentStatus,
)
from knowledge_index.contracts import KnowledgeReadResult, KnowledgeSnapshotKey
from rca_persistence.contracts import (
    ArtifactProvenance,
    AttemptLineageRead,
    CurrentRcaRead,
    RcaAggregate,
    RcaVersion,
    TypedArtifactProvenanceRead,
)


class IncidentPublicReads(Protocol):
    def event_has_incident_owner(self, event_id: str) -> bool: ...
    def list_incidents_by_workflow_status(self, status: IncidentStatus) -> tuple[IncidentRecord, ...]: ...
    def get_incident(self, incident_id: str) -> IncidentRecord | None: ...
    def get_rca_relationship(self, incident_id: str) -> IncidentRcaRelationship | None: ...


class RcaPublicReads(Protocol):
    def get_aggregate_by_incident(self, incident_id: str) -> RcaAggregate | None: ...
    def get_current(self, aggregate_id: str) -> CurrentRcaRead | None: ...
    def get_version_history(self, aggregate_id: str) -> tuple[RcaVersion, ...]: ...
    def get_artifact_provenance(
        self, version_id: str,
    ) -> ArtifactProvenance | TypedArtifactProvenanceRead | None: ...
    def get_attempt_lineage(self, attempt_id: str) -> AttemptLineageRead | None: ...


class EvidencePublicReads(Protocol):
    def resolve_snapshot(self, snapshot_id: str) -> EvidenceSnapshot | None: ...
    def resolve_revision(self, revision_id: str) -> EvidenceRevision | None: ...


class KnowledgePublicReads(Protocol):
    def read_snapshot(self, key: KnowledgeSnapshotKey) -> KnowledgeReadResult: ...
    def read_provenance(self, key: KnowledgeSnapshotKey) -> KnowledgeReadResult: ...


class GenerationPublicReads(Protocol):
    def result(self, validated_result_id: str) -> object: ...
