"""Host composition for SPEC-012/SPEC-008 publication semantics.

This module owns no scheduler, retry budget, clock, durable work, startup
barrier, or domain persistence.  A caller from the singular SPEC-011 Runtime
supplies authoritative time and decides when to invoke publication or
reconciliation.
"""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from incident_management import (
    IncidentRcaPublicationDisposition,
    IncidentRcaPublicationRequest,
    IncidentRcaPublicationResult,
    IncidentRcaRelationship,
)
from rca_persistence import (
    CurrentRcaRead,
    PublicationDisposition,
    PublicationResult,
    PublicationTargetIdentity,
    RcaVersion,
    RcaDomainError,
    RcaErrorCode,
    RecoveryCandidate,
    RecoveryCandidateKind,
)


class CandidateAPublicationPort(Protocol):
    def get_publication_result(
        self, publication_operation_id: str
    ) -> PublicationResult | None: ...

    def get_current(self, aggregate_id: str) -> CurrentRcaRead | None: ...

    def get_version_history(self, aggregate_id: str) -> tuple[RcaVersion, ...]: ...

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


class E4Classification(str, Enum):
    APPLIED = "APPLIED"
    PRECONDITION_SUPERSEDED = "PRECONDITION_SUPERSEDED"
    TARGET_ALREADY_CURRENT_EQUIVALENT = "TARGET_ALREADY_CURRENT_EQUIVALENT"
    TARGET_CONFLICT = "TARGET_CONFLICT"
    RESPONSE_LOST = "RESPONSE_LOST"
    A_COMMITTED_008_INCOMPLETE = "A_COMMITTED_008_INCOMPLETE"
    SPEC_008_APPLIED_A_INCOMPLETE = "SPEC_008_APPLIED_A_INCOMPLETE"
    RUNTIME_BOOKKEEPING_LOST = "RUNTIME_BOOKKEEPING_LOST"
    INCOHERENT_AUTHORITY = "INCOHERENT_AUTHORITY"


@dataclass(frozen=True, slots=True)
class PublicationInspection:
    incident_result: IncidentRcaPublicationResult | None
    relationship: IncidentRcaRelationship | None
    local_result: PublicationResult | None
    current: CurrentRcaRead | None
    history: tuple[RcaVersion, ...]
    classification: E4Classification

    @property
    def recovery_facts(self) -> frozenset[E4Classification]:
        facts = {self.classification}
        if (self.incident_result is not None and self.local_result is not None and
                self.local_result.disposition is PublicationDisposition.A_SIDE_COMMITTED):
            facts.add(E4Classification.RESPONSE_LOST)
        return frozenset(facts)


class RcaPublicationObservationUnstable(RuntimeError):
    """Ordered public reads changed during classification; caller may re-enter."""


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
        return self.reconcile(publication_operation_id, authoritative_now)

    def inspect(self, publication_operation_id: str, *,
                target: PublicationTargetIdentity | None = None) -> PublicationInspection:
        """Read E4 business authority in the frozen receipt-first order.

        An operation-only caller needs one A intent lookup to discover the
        Incident and Aggregate; it is reread at its ordered E4 position.
        """
        if target is None:
            target = self._require_local_publication(publication_operation_id).target
        if target.publication_operation_id != publication_operation_id:
            self._repair(target, "publication operation and target disagree")
        prior = None
        for _ in range(3):
            observed = self._read_e4_once(target)
            if observed.classification not in (E4Classification.TARGET_CONFLICT,
                                               E4Classification.INCOHERENT_AUTHORITY):
                return observed
            if self._hard_contradiction(target, observed):
                return observed
            if prior == observed:
                return observed
            prior = observed
        raise RcaPublicationObservationUnstable(
            "publication authority changed during bounded receipt-first reads")

    def _read_e4_once(self, target: PublicationTargetIdentity) -> PublicationInspection:
        publication_operation_id = target.publication_operation_id
        receipt = self._incident_reads.get_rca_publication_result(publication_operation_id)
        relationship = self._incident_reads.get_rca_relationship(target.incident_id)
        local = self._candidate_a.get_publication_result(publication_operation_id)
        current = self._candidate_a.get_current(target.aggregate_id)
        history = self._candidate_a.get_version_history(target.aggregate_id)
        classification = self._classify(target, receipt, relationship, local, current, history)
        return PublicationInspection(receipt, relationship, local, current, history, classification)

    @staticmethod
    def _hard_contradiction(target: PublicationTargetIdentity,
                            observed: PublicationInspection) -> bool:
        receipt, local = observed.incident_result, observed.local_result
        if receipt is not None and (receipt.replay_identity != (
                target.publication_operation_id, target.incident_id,
                target.target_version_id, target.expected_current_version_id)
                or receipt.disposition in (IncidentRcaPublicationDisposition.TARGET_ALREADY_CURRENT_CONFLICT,
                                           IncidentRcaPublicationDisposition.REPAIR_REQUIRED)):
            return True
        if local is None or local.target != target:
            return True
        versions = tuple(v for v in observed.history if v.version_id == target.target_version_id)
        if (len(versions) != 1 or versions[0].aggregate_id != target.aggregate_id or
                versions[0].publication_operation_id != target.publication_operation_id):
            return True
        if receipt is not None and local.disposition is not PublicationDisposition.A_SIDE_COMMITTED:
            return local != PublicationResult(
                target, PublicationDisposition(receipt.disposition.value),
                receipt.completed_at, receipt.resulting_current_version_id)
        return False

    def reconcile(
        self, publication_operation_id: str, authoritative_now: datetime, *,
        target: PublicationTargetIdentity | None = None,
    ) -> PublicationResult:
        observed = self.inspect(publication_operation_id, target=target)
        local = observed.local_result
        if local is None:
            raise RcaDomainError(RcaErrorCode.INVALID_REFERENCE,
                                 "publication operation has no Candidate-A durable authority",
                                 operation_id=publication_operation_id)
        target = local.target
        if observed.classification in (E4Classification.TARGET_CONFLICT,
                                       E4Classification.INCOHERENT_AUTHORITY):
            self._repair(target, observed.classification.value)
        if observed.classification is E4Classification.APPLIED:
            # This operation succeeded historically; a later, coherent
            # publication is Current.  Replaying the old receipt must not
            # attempt to promote the historical target again.
            return local
        incident_result = observed.incident_result
        if incident_result is None:
            # SPEC-008 atomically decides a concurrent same-ID replay or a
            # different-target race.  Read its durable typed receipt afterward.
            self._incident_mutations.publish_rca_current(
                _incident_request(target, authoritative_now))
            incident_result = self._incident_reads.get_rca_publication_result(publication_operation_id)
            if incident_result is None:
                self._repair(target, "SPEC-008 mutation has no durable typed receipt")
        relationship = self._incident_reads.get_rca_relationship(target.incident_id)
        self._validate_incident_result(target, incident_result, relationship)
        completed = self._complete(target, incident_result)
        final = self.inspect(publication_operation_id, target=target)
        if (final.local_result != completed or final.incident_result != incident_result or
                final.classification in (E4Classification.TARGET_CONFLICT,
                                         E4Classification.INCOHERENT_AUTHORITY,
                                         E4Classification.SPEC_008_APPLIED_A_INCOMPLETE)):
            self._repair(target, "A and SPEC-008 publication are not coherent")
        return completed

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

    @staticmethod
    def _classify(target: PublicationTargetIdentity,
                  receipt: IncidentRcaPublicationResult | None,
                  relationship: IncidentRcaRelationship | None,
                  local: PublicationResult | None,
                  current: CurrentRcaRead | None,
                  history: tuple[RcaVersion, ...]) -> E4Classification:
        if relationship is None or relationship.incident_id != target.incident_id:
            return E4Classification.INCOHERENT_AUTHORITY
        if local is None or local.target != target:
            return E4Classification.INCOHERENT_AUTHORITY
        versions = tuple(v for v in history if v.version_id == target.target_version_id)
        if (len(versions) != 1 or versions[0].aggregate_id != target.aggregate_id or
                versions[0].publication_operation_id != target.publication_operation_id):
            return E4Classification.INCOHERENT_AUTHORITY
        a_current = None if current is None else current.version.version_id
        if current is not None and (current.current.aggregate_id != target.aggregate_id or
                                    current.version.aggregate_id != target.aggregate_id or
                                    current.publication_result.disposition is not PublicationDisposition.APPLIED):
            return E4Classification.INCOHERENT_AUTHORITY
        incident_current = relationship.current_version_id
        if receipt is None:
            if local.disposition is not PublicationDisposition.A_SIDE_COMMITTED:
                return E4Classification.INCOHERENT_AUTHORITY
            if incident_current == target.target_version_id:
                # Current alone does not establish same-operation equivalence.
                return E4Classification.TARGET_CONFLICT
            if incident_current != a_current:
                return E4Classification.INCOHERENT_AUTHORITY
            return E4Classification.A_COMMITTED_008_INCOMPLETE
        if receipt.replay_identity != (target.publication_operation_id, target.incident_id,
                                       target.target_version_id, target.expected_current_version_id):
            return E4Classification.TARGET_CONFLICT
        if local.disposition is not PublicationDisposition.A_SIDE_COMMITTED:
            expected_local = PublicationResult(
                target, PublicationDisposition(receipt.disposition.value),
                receipt.completed_at, receipt.resulting_current_version_id)
            if local != expected_local:
                return E4Classification.INCOHERENT_AUTHORITY
        if receipt.disposition is IncidentRcaPublicationDisposition.TARGET_ALREADY_CURRENT_CONFLICT:
            return E4Classification.TARGET_CONFLICT
        if receipt.disposition is IncidentRcaPublicationDisposition.APPLIED:
            if receipt.resulting_current_version_id != target.target_version_id:
                return E4Classification.INCOHERENT_AUTHORITY
            if incident_current != target.target_version_id:
                # A later publication is coherent only if A also shows the
                # exact current relationship through its public Current read.
                if (a_current != incident_current or current is None or
                        current.publication_result.target.target_version_id != incident_current):
                    return E4Classification.INCOHERENT_AUTHORITY
            if local.disposition is PublicationDisposition.A_SIDE_COMMITTED:
                if incident_current != target.target_version_id:
                    return E4Classification.INCOHERENT_AUTHORITY
                return E4Classification.SPEC_008_APPLIED_A_INCOMPLETE
            if (local.disposition is not PublicationDisposition.APPLIED or
                    local.resulting_current_version_id != target.target_version_id or
                    a_current != incident_current):
                return E4Classification.INCOHERENT_AUTHORITY
            return (E4Classification.TARGET_ALREADY_CURRENT_EQUIVALENT
                    if incident_current == target.target_version_id else E4Classification.APPLIED)
        if receipt.disposition is IncidentRcaPublicationDisposition.PRECONDITION_SUPERSEDED:
            if (incident_current != receipt.resulting_current_version_id or
                    incident_current == target.target_version_id or
                    a_current != incident_current or
                    local.disposition not in (PublicationDisposition.A_SIDE_COMMITTED,
                                              PublicationDisposition.PRECONDITION_SUPERSEDED)):
                return E4Classification.INCOHERENT_AUTHORITY
            return E4Classification.PRECONDITION_SUPERSEDED
        return E4Classification.INCOHERENT_AUTHORITY

    @staticmethod
    def _validate_incident_result(target: PublicationTargetIdentity,
                                  result: IncidentRcaPublicationResult,
                                  relationship: IncidentRcaRelationship | None) -> None:
        if (result.replay_identity != (target.publication_operation_id, target.incident_id,
                                       target.target_version_id, target.expected_current_version_id) or
                relationship is None or relationship.incident_id != target.incident_id or
                relationship.current_version_id != result.resulting_current_version_id or
                result.disposition in (IncidentRcaPublicationDisposition.TARGET_ALREADY_CURRENT_CONFLICT,
                                       IncidentRcaPublicationDisposition.REPAIR_REQUIRED)):
            RcaPublicationCoordinator._repair(target, "SPEC-008 receipt and relationship contradict")

    @staticmethod
    def _repair(target: PublicationTargetIdentity, message: str) -> None:
        raise RcaDomainError(RcaErrorCode.REPAIR_REQUIRED, message,
                             operation_id=target.publication_operation_id,
                             aggregate_id=target.aggregate_id,
                             version_id=target.target_version_id)

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
