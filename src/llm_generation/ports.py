"""Consumer-owned, read-only public semantic capabilities from A, B and C."""

from __future__ import annotations

from typing import Protocol

from incident_evidence.contracts import EvidenceRevision, EvidenceSnapshot
from knowledge_index.contracts import KnowledgeReadResult, KnowledgeSnapshotKey
from rca_persistence.contracts import AttemptLineageRead


class AttemptPublicReader(Protocol):
    def get_attempt_lineage(self, attempt_id: str) -> AttemptLineageRead | None: ...


class EvidencePublicReader(Protocol):
    def resolve_snapshot(self, snapshot_id: str) -> EvidenceSnapshot | None: ...
    def resolve_revision(self, revision_id: str) -> EvidenceRevision | None: ...


class KnowledgePublicReader(Protocol):
    def read_snapshot(self, key: KnowledgeSnapshotKey) -> KnowledgeReadResult: ...
    def read_provenance(self, key: KnowledgeSnapshotKey) -> KnowledgeReadResult: ...
