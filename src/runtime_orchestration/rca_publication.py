"""SPEC-016 S5 publication reconciliation before Runtime completion.

This is invoked by the existing Runtime.  It owns no scheduler, retry loop,
clock, or startup dispatch.  Its D2 reads follow the four business reads in
the E4 authority order.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Protocol

from rca_integration.publication import (
    E4Classification, PublicationInspection, RcaPublicationCoordinator,
)
from rca_persistence import (
    AttemptLineageRead, GenerationLifecycle, LogicalTryResultKind,
    PublicationDisposition, PublicationResult, PublicationTargetIdentity,
)

from .contracts import RuntimeWorkRecord, RuntimeWorkStatus, RuntimeWorkStore
from .contracts import RuntimeWorkKind
from .identity import runtime_work_id
from .rca_continuation import (
    RcaContinuation, RcaContinuationConcurrencyError,
    ContradictoryRcaContinuationError,
    SqliteRcaContinuationStore, rca_root_id,
)
from .sqlite_work_store import RuntimeWorkConcurrencyError
from .rca_followup import FollowUpDisposition, FollowUpResult


class FollowUpRecheckPort(Protocol):
    def recheck(self, incident_id: str) -> FollowUpResult: ...


class IncidentRecordReadPort(Protocol):
    def get_incident(self, incident_id: str) -> object | None: ...


class PublicationRuntimeDisposition(str, Enum):
    COMPLETE = "COMPLETE"
    OUTSTANDING = "OUTSTANDING"
    RUNTIME_BOOKKEEPING_LOST = "RUNTIME_BOOKKEEPING_LOST"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


@dataclass(frozen=True, slots=True)
class PublicationRuntimeResult:
    disposition: PublicationRuntimeDisposition
    classification: E4Classification
    publication: PublicationResult | None = None
    continuation: RcaContinuation | None = None
    work: RuntimeWorkRecord | None = None


def advance_publication_continuation(
    candidate_a: object, continuations: SqliteRcaContinuationStore,
    target: PublicationTargetIdentity, authoritative_now: datetime, *,
    expected_work: RuntimeWorkRecord | None = None,
) -> RcaContinuation:
    """Fresh-read A's durable handoff, then CAS only E-owned continuity.

    The target is A's exact public intent, including its original precondition.
    Neither recovery nor this transition derives a replacement publication.
    """
    root = rca_root_id(target.incident_id)
    while True:
        state = continuations.get(root)
        candidate_a.validate_local_readiness()
        receipt = candidate_a.get_publication_result(target.publication_operation_id)
        version = candidate_a.get_version(target.target_version_id)
        aggregate = candidate_a.get_aggregate(target.aggregate_id)
        view = (candidate_a.get_attempt_lineage(version.attempt_id)
                if version is not None else None)
        if (state is None or receipt is None or receipt.target != target
            or version is None or version.version_id != target.target_version_id
            or version.aggregate_id != target.aggregate_id
            or version.publication_operation_id != target.publication_operation_id
            or aggregate is None or aggregate.incident_id != target.incident_id
            or not isinstance(view, AttemptLineageRead)
            or view.attempt.lifecycle is not GenerationLifecycle.COMPLETED
            or not view.try_outcomes
            or view.try_outcomes[-1].result_kind is not LogicalTryResultKind.VALIDATED_RESULT
            or candidate_a.get_artifact(version.version_id) != version.artifact):
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: publication lacks durable A handoff")
        lineage, provenance = view.attempt.lineage, version.artifact.provenance
        admitted_attempt = (state.refresh_basis.attempt_id if state.refresh_basis is not None
                            else state.attempt_id)
        if (lineage.attempt_id != version.attempt_id or lineage.aggregate_id != target.aggregate_id
            or (lineage.evidence_snapshot_id, lineage.evidence_revision_id,
                lineage.knowledge_snapshot_id, lineage.generation_provenance) !=
               (provenance.evidence_snapshot_id, provenance.evidence_revision_id,
                provenance.knowledge_snapshot_id, provenance.generation)
            or state.incident_id != target.incident_id
            or state.aggregate_id != target.aggregate_id
            or admitted_attempt != version.attempt_id
            or state.stage not in ("EXECUTION", "PUBLICATION")):
            raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: publication lineage or binding contradicts A")
        replacing = state.publication_operation_id not in (None, target.publication_operation_id)
        if replacing:
            prior = candidate_a.get_publication_result(state.publication_operation_id)
            refresh = state.refresh_basis
            if (refresh is None or prior is None
                or prior.disposition is not PublicationDisposition.APPLIED
                or prior.target.incident_id != target.incident_id
                or prior.target.aggregate_id != target.aggregate_id
                or prior.target.target_version_id != refresh.baseline_version_id
                or target.expected_current_version_id != refresh.baseline_version_id
                or refresh.attempt_id != version.attempt_id
                or (refresh.evidence_snapshot_id, refresh.evidence_revision_id,
                    refresh.knowledge_snapshot_id) != (lineage.evidence_snapshot_id,
                    lineage.evidence_revision_id, lineage.knowledge_snapshot_id)):
                raise ContradictoryRcaContinuationError("REPAIR_REQUIRED: publication binding is not a resolved refresh predecessor")
        if (state.stage == "PUBLICATION" and state.next_action == "PUBLICATION"
            and state.publication_operation_id == target.publication_operation_id):
            return state
        active = continuations.get_follow_up_work(root)
        if expected_work is not None and active != expected_work:
            raise RuntimeWorkConcurrencyError("RCA publication lost Runtime fencing authority")
        observed = max(authoritative_now, state.updated_at, state.observed_at)
        try:
            return continuations.advance_publication(
                replace(state, stage="PUBLICATION", next_action="PUBLICATION",
                        publication_operation_id=target.publication_operation_id,
                        updated_at=observed, observed_at=observed),
                expected_revision=state.revision, expected_work=active,
                replace_resolved_publication=replacing)
        except RcaContinuationConcurrencyError:
            # Preserve a concurrent frontier admission; re-prove A and the
            # exact binding rather than overwrite the winning continuation.
            continue


class RcaPublicationOrchestrator:
    """One caller-driven S5 step, with terminal Runtime bookkeeping last."""

    def __init__(self, coordinator: RcaPublicationCoordinator,
                 continuations: SqliteRcaContinuationStore,
                 runtime_work: RuntimeWorkStore,
                 follow_up: FollowUpRecheckPort,
                 incident_records: IncidentRecordReadPort | None = None,
                 candidate_a: object | None = None) -> None:
        self._publication = coordinator
        self._continuations = continuations
        self._runtime_work = runtime_work
        self._follow_up = follow_up
        self._incident_records = incident_records
        self._candidate_a = candidate_a

    def inspect(self, target: PublicationTargetIdentity) -> tuple[PublicationInspection, RcaContinuation | None]:
        business = self._publication.inspect(target.publication_operation_id, target=target)
        continuation = self._continuations.get(rca_root_id(target.incident_id))
        return business, continuation

    def reconcile(self, target: PublicationTargetIdentity,
                  authoritative_now: datetime) -> PublicationRuntimeResult:
        business, continuation = self.inspect(target)
        if business.classification in (E4Classification.TARGET_CONFLICT,
                                       E4Classification.INCOHERENT_AUTHORITY):
            return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                                            business.classification, continuation=continuation)
        if continuation is not None and (continuation.stage == "EXECUTION" or
            (self._candidate_a is not None and continuation.stage == "PUBLICATION"
             and continuation.publication_operation_id != target.publication_operation_id)):
            # E4 business inspection above precedes D2 advancement. A's
            # durable Artifact/Version/intent proves this interrupted handoff;
            # EXECUTION alone is never sufficient authorization.
            try:
                if self._candidate_a is None:
                    raise ContradictoryRcaContinuationError("A handoff authority is unavailable")
                continuation = advance_publication_continuation(
                    self._candidate_a, self._continuations, target, authoritative_now)
            except (ContradictoryRcaContinuationError, ValueError, TypeError, AttributeError):
                return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                    E4Classification.INCOHERENT_AUTHORITY, continuation=continuation)
            except RuntimeWorkConcurrencyError:
                return PublicationRuntimeResult(PublicationRuntimeDisposition.OUTSTANDING,
                    business.classification, continuation=continuation)
        # Domain reconciliation precedes renewed generation or refresh, even
        # when D2 bookkeeping needs later reconstruction.
        publication = self._publication.reconcile(target.publication_operation_id,
                                                  authoritative_now, target=target)
        coherent, continuation = self.inspect(target)
        if coherent.classification in (E4Classification.TARGET_CONFLICT,
                                       E4Classification.INCOHERENT_AUTHORITY,
                                       E4Classification.SPEC_008_APPLIED_A_INCOMPLETE):
            return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                                            coherent.classification, publication, continuation)
        if continuation is None:
            # No public fact can reconstruct an unknown admitted frontier.
            # Keep domain publication coherent; prohibit false Runtime success.
            return PublicationRuntimeResult(PublicationRuntimeDisposition.RUNTIME_BOOKKEEPING_LOST,
                                            E4Classification.RUNTIME_BOOKKEEPING_LOST, publication)
        if (continuation.publication_operation_id is None and
                continuation.stage == "PUBLICATION" and
                (continuation.aggregate_id is None or
                 continuation.aggregate_id == target.aggregate_id)):
            # A's immutable intent can restore an omitted D2 reference.  The
            # CAS retains the existing typed frontier and absolute wake.
            observed_at = max(authoritative_now, continuation.updated_at)
            candidate = replace(continuation,
                                publication_operation_id=target.publication_operation_id,
                                updated_at=observed_at, observed_at=observed_at)
            try:
                continuation = self._continuations.update(candidate,
                    expected_revision=continuation.revision)
            except RcaContinuationConcurrencyError:
                continuation = self._continuations.get(rca_root_id(target.incident_id))
        if (continuation.incident_id != target.incident_id or
                (continuation.aggregate_id is not None and
                 continuation.aggregate_id != target.aggregate_id) or
                continuation.publication_operation_id != target.publication_operation_id):
            return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                                            E4Classification.INCOHERENT_AUTHORITY, publication,
                                            continuation)
        if self._candidate_a is not None:
            # A committed handoff may have interrupted E before terminal
            # generation bookkeeping. Preserve and finish the existing budget
            # after business reconciliation, never recreate its consumption.
            version = self._candidate_a.get_version(target.target_version_id)
            budget_id = runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT,
                                       continuation.root_id, version.attempt_id)
            budget = self._runtime_work.get(budget_id)
            active = self._continuations.get_follow_up_work(continuation.root_id)
            if (budget is None or budget.work_kind is not RuntimeWorkKind.RCA_ATTEMPT
                or budget.incident_id != target.incident_id
                or budget.operation_id != version.attempt_id
                or budget.workflow_operation_id != continuation.root_id
                or budget.status not in (RuntimeWorkStatus.OUTSTANDING, RuntimeWorkStatus.COMPLETED)):
                return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                    E4Classification.INCOHERENT_AUTHORITY, publication, continuation)
            if budget.status is RuntimeWorkStatus.OUTSTANDING:
                if active.status is not RuntimeWorkStatus.OUTSTANDING:
                    return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                        E4Classification.INCOHERENT_AUTHORITY, publication, continuation)
                try:
                    self._continuations.complete_publication_budget(continuation.root_id, budget,
                        expected_work=active, expected_revision=continuation.revision,
                        observed_at=max(authoritative_now, budget.updated_at),
                    )
                except (RuntimeWorkConcurrencyError, RcaContinuationConcurrencyError):
                    return PublicationRuntimeResult(PublicationRuntimeDisposition.OUTSTANDING,
                        coherent.classification, publication, continuation)
        checked = self._follow_up.recheck(target.incident_id)
        if checked.disposition is FollowUpDisposition.REPAIR_REQUIRED:
            return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                                            E4Classification.INCOHERENT_AUTHORITY, publication,
                                            continuation)
        if checked.disposition is not FollowUpDisposition.COMPLETE:
            return PublicationRuntimeResult(PublicationRuntimeDisposition.OUTSTANDING,
                                            coherent.classification, publication,
                                            checked.continuation or continuation)
        fresh, latest = self.inspect(target)
        if (latest is None or checked.continuation is None or
                latest.revision != checked.continuation.revision or
                latest.unresolved_frontier or
                (latest.admitted_frontier and not latest.follow_up_complete) or
                fresh.classification in (E4Classification.TARGET_CONFLICT,
                                         E4Classification.INCOHERENT_AUTHORITY,
                                         E4Classification.SPEC_008_APPLIED_A_INCOMPLETE) or
                fresh.local_result != publication):
            return PublicationRuntimeResult(PublicationRuntimeDisposition.OUTSTANDING,
                                            fresh.classification, publication, latest)
        active_work_id = latest.follow_up_work_id or latest.runtime_work_id
        work = self._runtime_work.get(active_work_id)
        if work is None:
            if latest.follow_up_work_id is not None:
                return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                                                E4Classification.INCOHERENT_AUTHORITY,
                                                publication, latest)
            work = self._reconstruct_work(latest, authoritative_now)
            if work is None:
                return PublicationRuntimeResult(PublicationRuntimeDisposition.RUNTIME_BOOKKEEPING_LOST,
                                                E4Classification.RUNTIME_BOOKKEEPING_LOST,
                                                publication, latest)
        if not self._work_binding_valid(work, latest, target):
            return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                                            E4Classification.INCOHERENT_AUTHORITY,
                                            publication, latest, work)
        try:
            bound = self._continuations.get_follow_up_work(latest.root_id)
        except ContradictoryRcaContinuationError:
            return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                                            E4Classification.INCOHERENT_AUTHORITY,
                                            publication, latest, work)
        if bound != work:
            return PublicationRuntimeResult(PublicationRuntimeDisposition.OUTSTANDING,
                                            coherent.classification, publication, latest, work)
        if work.status is not RuntimeWorkStatus.COMPLETED:
            if work.is_terminal or authoritative_now < work.updated_at:
                return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                                                E4Classification.INCOHERENT_AUTHORITY,
                                                publication, latest, work)
            try:
                work = self._continuations.complete_rca_follow_up_if_frontier_unchanged(
                    latest.root_id, expected_work_revision=work.revision,
                    expected_frontier_revision=latest.revision,
                    observed_at=authoritative_now)
            except (RcaContinuationConcurrencyError, RuntimeWorkConcurrencyError):
                return PublicationRuntimeResult(PublicationRuntimeDisposition.OUTSTANDING,
                                                coherent.classification, publication,
                                                self._continuations.get(latest.root_id))
            except ContradictoryRcaContinuationError:
                return PublicationRuntimeResult(PublicationRuntimeDisposition.REPAIR_REQUIRED,
                                                E4Classification.INCOHERENT_AUTHORITY,
                                                publication, latest, work)
        # Terminal bookkeeping is never treated as business truth on replay.
        verified, final = self.inspect(target)
        if (final is None or final.revision != latest.revision or
                verified.local_result != publication or
                verified.classification in (E4Classification.TARGET_CONFLICT,
                                            E4Classification.INCOHERENT_AUTHORITY,
                                            E4Classification.SPEC_008_APPLIED_A_INCOMPLETE)):
            return PublicationRuntimeResult(PublicationRuntimeDisposition.OUTSTANDING,
                                            verified.classification, publication, final, work)
        return PublicationRuntimeResult(PublicationRuntimeDisposition.COMPLETE,
                                        verified.classification, publication, final, work)

    def _work_binding_valid(self, work: RuntimeWorkRecord,
                            continuation: RcaContinuation,
                            target: PublicationTargetIdentity) -> bool:
        root = rca_root_id(target.incident_id)
        expected_work_id = continuation.follow_up_work_id or runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root)
        expected_kind = (RuntimeWorkKind.RCA_FOLLOW_UP if continuation.follow_up_work_id
                         else RuntimeWorkKind.RCA_INITIAL)
        expected_operation = (continuation.follow_up_root_id if continuation.follow_up_work_id
                              else root)
        if (self._incident_records is None or continuation.root_id != root or
                continuation.runtime_work_id != runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root) or
                work.work_id != expected_work_id or
                work.work_kind is not expected_kind or
                work.incident_id != target.incident_id or
                work.operation_id != expected_operation or work.workflow_operation_id is not None or
                work.retry_limit != continuation.retry_limit or
                (not continuation.follow_up_work_id and
                 work.created_at != continuation.created_at)):
            return False
        try:
            incident = self._incident_records.get_incident(target.incident_id)
        except Exception:
            return False
        event_id = None if incident is None else getattr(incident, "anchor_event_id", None)
        return (incident is not None and getattr(incident, "incident_id", None) == target.incident_id
                and isinstance(event_id, str) and bool(event_id) and work.event_id == event_id)

    def _reconstruct_work(self, continuation: RcaContinuation,
                          authoritative_now: datetime) -> RuntimeWorkRecord | None:
        if self._incident_records is None:
            return None
        root = rca_root_id(continuation.incident_id)
        expected_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root)
        if continuation.runtime_work_id != expected_id:
            return None
        incident = self._incident_records.get_incident(continuation.incident_id)
        if incident is None or getattr(incident, "incident_id", None) != continuation.incident_id:
            return None
        event_id = getattr(incident, "anchor_event_id", None)
        if not isinstance(event_id, str) or not event_id:
            return None
        enumeration = self._runtime_work.enumerate_all()
        if enumeration.isolated_corruptions or any(
            item.work_kind is RuntimeWorkKind.RCA_INITIAL and
            (item.incident_id == continuation.incident_id or item.operation_id == root)
            for item in enumeration.records
        ):
            return None
        if authoritative_now < continuation.created_at:
            return None
        proposed = RuntimeWorkRecord(
            expected_id, RuntimeWorkKind.RCA_INITIAL, event_id,
            "PUBLICATION", "PUBLICATION", 0, continuation.retry_limit,
            RuntimeWorkStatus.OUTSTANDING, continuation.created_at,
            continuation.created_at, continuation.created_at,
            incident_id=continuation.incident_id, operation_id=root,
        )
        try:
            return self._runtime_work.create(proposed)
        except Exception:
            # A concurrent constructor may have won; only the public read can
            # prove an equivalent Runtime record.  Other failures fail closed.
            existing = self._runtime_work.get(expected_id)
            if (existing is not None and existing.work_kind is RuntimeWorkKind.RCA_INITIAL
                    and existing.event_id == event_id and existing.incident_id == continuation.incident_id
                    and existing.operation_id == root and existing.retry_limit == continuation.retry_limit):
                return existing
            raise
