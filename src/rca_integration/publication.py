"""Host composition for SPEC-012/SPEC-008 publication semantics.

This module owns no scheduler, retry budget, clock, durable work, startup
barrier, or domain persistence.  A caller from the singular SPEC-011 Runtime
supplies authoritative time and decides when to invoke publication or
reconciliation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from incident_management import (
    IncidentRcaPublicationRequest,
    IncidentRcaPublicationResult,
    IncidentRcaRelationship,
)
from rca_persistence import (
    PublicationDisposition,
    PublicationResult,
    PublicationTargetIdentity,
    RcaDomainError,
    RcaErrorCode,
    RecoveryCandidate,
    RecoveryCandidateKind,
)


class CandidateAPublicationPort(Protocol):
    def get_publication_result(
        self, publication_operation_id: str
    ) -> PublicationResult | None: ...

    def complete_authorized_publication(
        self, result: PublicationResult
    ) -> PublicationResult: ...

    def enumerate_recovery_candidates(self) -> tuple[RecoveryCandidate, ...]: ...


class IncidentRcaPublicationMutationPort(Protocol):
    def publish_rca_current(
        self, request: IncidentRcaPublicationRequest
    ) -> IncidentRcaPublicationResult: ...


class IncidentRcaPublicationReadPort(Protocol):
    def get_rca_relationship(
        self, incident_id: str
    ) -> IncidentRcaRelationship | None: ...

    def get_rca_publication_result(
        self, publication_operation_id: str
    ) -> IncidentRcaPublicationResult | None: ...


class RcaPublicationCoordinator:
    """Caller-driven cross-domain protocol over public semantic ports only."""

    def __init__(
        self,
        candidate_a: CandidateAPublicationPort,
        incident_mutations: IncidentRcaPublicationMutationPort,
        incident_reads: IncidentRcaPublicationReadPort,
    ) -> None:
        self._candidate_a = candidate_a
        self._incident_mutations = incident_mutations
        self._incident_reads = incident_reads

    def publish(
        self, publication_operation_id: str, authoritative_now: datetime
    ) -> PublicationResult:
        local = self._require_local_publication(publication_operation_id)
        self._require_incident(local.target)
        incident_result = self._incident_mutations.publish_rca_current(
            _incident_request(local.target, authoritative_now)
        )
        return self._complete(local.target, incident_result)

    def reconcile(
        self, publication_operation_id: str, authoritative_now: datetime
    ) -> PublicationResult:
        local = self._require_local_publication(publication_operation_id)
        self._require_incident(local.target)
        incident_result = self._incident_reads.get_rca_publication_result(
            publication_operation_id
        )
        if incident_result is None:
            incident_result = self._incident_mutations.publish_rca_current(
                _incident_request(local.target, authoritative_now)
            )
        return self._complete(local.target, incident_result)

    def reconcile_outstanding(
        self, authoritative_now: datetime
    ) -> tuple[PublicationResult, ...]:
        publication_kinds = {
            RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION,
            RecoveryCandidateKind.UNRESOLVED_PUBLICATION,
        }
        operation_ids = tuple(
            candidate.publication_operation_id
            for candidate in self._candidate_a.enumerate_recovery_candidates()
            if candidate.kind in publication_kinds
            and candidate.publication_operation_id is not None
        )
        return tuple(
            self.reconcile(operation_id, authoritative_now)
            for operation_id in operation_ids
        )

    def _require_local_publication(
        self, publication_operation_id: str
    ) -> PublicationResult:
        local = self._candidate_a.get_publication_result(
            publication_operation_id
        )
        if local is None:
            raise RcaDomainError(
                RcaErrorCode.INVALID_REFERENCE,
                "publication operation has no Candidate-A durable authority",
                operation_id=publication_operation_id,
            )
        return local

    def _require_incident(self, target: PublicationTargetIdentity) -> None:
        relationship = self._incident_reads.get_rca_relationship(
            target.incident_id
        )
        if relationship is None:
            raise RcaDomainError(
                RcaErrorCode.INVALID_REFERENCE,
                "publication target references an unknown Incident",
                operation_id=target.publication_operation_id,
                aggregate_id=target.aggregate_id,
                version_id=target.target_version_id,
            )

    def _complete(
        self,
        target: PublicationTargetIdentity,
        incident_result: IncidentRcaPublicationResult,
    ) -> PublicationResult:
        if incident_result.replay_identity != (
            target.publication_operation_id,
            target.incident_id,
            target.target_version_id,
            target.expected_current_version_id,
        ):
            raise RcaDomainError(
                RcaErrorCode.PUBLICATION_EVIDENCE_INCONSISTENCY,
                "Incident publication result contradicts Candidate-A target",
                operation_id=target.publication_operation_id,
                aggregate_id=target.aggregate_id,
                version_id=target.target_version_id,
            )
        result = PublicationResult(
            target=target,
            disposition=PublicationDisposition(incident_result.disposition.value),
            recorded_at=incident_result.completed_at,
            resulting_current_version_id=(
                incident_result.resulting_current_version_id
            ),
        )
        return self._candidate_a.complete_authorized_publication(result)


def _incident_request(
    target: PublicationTargetIdentity, authoritative_now: datetime
) -> IncidentRcaPublicationRequest:
    return IncidentRcaPublicationRequest(
        publication_operation_id=target.publication_operation_id,
        incident_id=target.incident_id,
        target_version_id=target.target_version_id,
        expected_current_version_id=target.expected_current_version_id,
        authoritative_now=authoritative_now,
    )
