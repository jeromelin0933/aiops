"""SPEC-016 S4 follow-up composition over public A/B/C and existing D2 facts."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
from typing import Mapping, Protocol

from incident_evidence.contracts import (
    EvidenceRevision, EvidenceSnapshot, MaterialityEvaluationKind,
    MaterialityJudgement, MaterialityRequest, MaterialityResult,
)
from incident_management.contracts import IncidentStatus
from knowledge_index.contracts import KnowledgeReadStatus, KnowledgeSnapshotKey, RetrievalResolution
from rca_persistence.contracts import (
    AdmitAttemptRequest, AttemptLineage, AttemptLineageRead, CurrentFreshness,
    CurrentRca, CurrentRcaRead, RcaDomainError,
)

from .clock import RuntimeClock, canonical_utc
from .contracts import RuntimeWorkKind
from .identity import runtime_work_id
from .rca_continuation import (
    ContradictoryRcaContinuationError, FollowUpRequirement, FollowUpResolution, RcaContinuation,
    RcaContinuationConcurrencyError, RefreshBasis, SqliteRcaContinuationStore,
    rca_child_operation_id, rca_root_id,
)
from .rca_initial import IncidentPublicAdapter, InitialRcaDisposition, derive_attempt_admission_operation_id


class FollowUpDisposition(str, Enum):
    OUTSTANDING = "OUTSTANDING"
    COMPLETE = "COMPLETE"
    ATTEMPT_ADMITTED = "ATTEMPT_ADMITTED"
    SUPPRESSED = "SUPPRESSED"
    WAITING = "WAITING"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class FollowUpResult:
    disposition: FollowUpDisposition
    continuation: RcaContinuation | None = None
    materiality: MaterialityResult | None = None


class _Repair(RuntimeError):
    pass


class _Unavailable(RuntimeError):
    pass


class _CurrentChanged(RuntimeError):
    """A public Current changed during B comparison; restart the full read."""
    pass


class CandidateAFollowUpPort(Protocol):
    def validate_local_readiness(self) -> None: ...
    def get_aggregate_by_incident(self, incident_id: str) -> object | None: ...
    def get_current(self, aggregate_id: str) -> CurrentRcaRead | None: ...
    def get_freshness_lineage(self, aggregate_id: str) -> tuple[CurrentRca, ...]: ...
    def get_attempt_lineage(self, attempt_id: str) -> AttemptLineageRead | None: ...
    def apply_authorized_freshness(self, current: CurrentRca) -> CurrentRca: ...
    def admit_attempt(self, request: AdmitAttemptRequest) -> object: ...


class CandidateBFollowUpPort(Protocol):
    def resolve_revision(self, revision_id: str) -> EvidenceRevision | None: ...
    def resolve_snapshot(self, snapshot_id: str) -> EvidenceSnapshot | None: ...
    def compare_materiality(self, request: MaterialityRequest) -> MaterialityResult: ...


class CandidateCFollowUpPort(Protocol):
    def read_snapshot(self, key: KnowledgeSnapshotKey) -> object: ...


_AUTO_REFRESH = frozenset({IncidentStatus.OPEN, IncidentStatus.ASSIGNED,
                           IncidentStatus.IN_PROGRESS})


class RcaFollowUpOrchestrator:
    """One stable follow-up identity; each D2 change is a loss-free CAS."""

    def __init__(self, *, incidents: object, candidate_a: CandidateAFollowUpPort,
                 candidate_b: CandidateBFollowUpPort, candidate_c: CandidateCFollowUpPort,
                 continuations: SqliteRcaContinuationStore, clock: RuntimeClock,
                 materiality_rule_version: str = "evidence-materiality-v1") -> None:
        self._incidents = IncidentPublicAdapter(incidents)
        self._a, self._b, self._c = candidate_a, candidate_b, candidate_c
        self._d2, self._clock = continuations, clock
        self._rule = materiality_rule_version

    @staticmethod
    def follow_up_root(incident_id: str) -> str:
        root = rca_root_id(incident_id)
        return rca_child_operation_id(root, incident_id, "FOLLOW_UP")

    def admit(self, incident_id: str, requirement: FollowUpRequirement) -> FollowUpResult:
        try:
            return FollowUpResult(FollowUpDisposition.OUTSTANDING,
                                  self._admit(incident_id, requirement))
        except _Unavailable:
            return FollowUpResult(FollowUpDisposition.UNAVAILABLE)
        except OSError:
            return FollowUpResult(FollowUpDisposition.UNAVAILABLE)
        except Exception:
            return FollowUpResult(FollowUpDisposition.REPAIR_REQUIRED)

    def _admit(self, incident_id: str, requirement: FollowUpRequirement, *,
               scheduled_requirement: FollowUpRequirement | None = None) -> RcaContinuation:
        if not isinstance(requirement, FollowUpRequirement):
            raise _Repair("invalid typed requirement")
        wake = self._member_wake(incident_id, requirement)
        root = rca_root_id(incident_id)
        follow_root = self.follow_up_root(incident_id)
        while True:
            incident, _, _ = self._subject(incident_id)
            state = self._state(root, incident_id)
            scheduled_stale = (
                incident.status is IncidentStatus.AWAITING_REVIEW
                and requirement.requirement_type == "STALE_REFRESH"
                and scheduled_requirement is not None
                and scheduled_requirement in state.unresolved_frontier
            )
            if (incident.status not in _AUTO_REFRESH and
                requirement not in state.admitted_frontier and not scheduled_stale):
                raise _Repair("lifecycle does not admit new automatic follow-up")
            if requirement.requirement_type == "STALE_REFRESH":
                self._require_stale_current(incident_id, requirement.reference[8:])
            if requirement in state.admitted_frontier:
                return state
            if len(state.admitted_frontier) >= 64:
                raise _Repair("typed frontier capacity reached")
            if requirement.requirement_type == "STALE_REFRESH" and any(
                item.requirement_type == "STALE_REFRESH" and item != requirement
                for item in state.unresolved_frontier
            ):
                raise _Repair("contradictory STALE baselines")
            preserved_wake = state.follow_up_wake_at
            due = state.next_eligibility_at
            if preserved_wake is not None:
                due = preserved_wake if due is None else min(due, preserved_wake)
            if wake is not None:
                due = wake if due is None else min(due, wake)
                preserved_wake = wake if preserved_wake is None else min(preserved_wake, wake)
            if (requirement.requirement_type == "POST_CONTEXT" and
                all(item.requirement_type == "POST_CONTEXT"
                    for item in state.unresolved_frontier)):
                # The old wake remains historical evidence. A newly admitted
                # B boundary governs the next post-context-only dispatch.
                due = max(self._member_wake(incident_id, item)
                          for item in state.unresolved_frontier + (requirement,))
            now = self._observation(state)
            candidate = replace(state, follow_up_root_id=follow_root,
                                unresolved_frontier=state.unresolved_frontier + (requirement,),
                                admitted_frontier=state.admitted_frontier + (requirement,),
                                next_eligibility_at=due, follow_up_wake_at=preserved_wake,
                                follow_up_complete=False,
                                updated_at=now, observed_at=now)
            try:
                return self._d2.admit_follow_up(candidate, expected_revision=state.revision)
            except RcaContinuationConcurrencyError:
                continue

    def recheck(self, incident_id: str, *,
                post_context_snapshots: Mapping[FollowUpRequirement, str] | None = None) -> FollowUpResult:
        """Reconcile every durable member with public authority before completion."""
        try:
            return self._recheck(incident_id, post_context_snapshots or {})
        except _Unavailable:
            return FollowUpResult(FollowUpDisposition.UNAVAILABLE)
        except OSError:
            return FollowUpResult(FollowUpDisposition.UNAVAILABLE)
        except Exception:
            return FollowUpResult(FollowUpDisposition.REPAIR_REQUIRED)

    def _recheck(self, incident_id: str,
                 post_context_snapshots: Mapping[FollowUpRequirement, str] | None = None) -> FollowUpResult:
        root = rca_root_id(incident_id)
        while True:
            self._subject(incident_id)
            state = self._state(root, incident_id)
            if len(state.follow_up_capture_bases) > len(state.follow_up_capture_snapshots):
                return FollowUpResult(FollowUpDisposition.OUTSTANDING, state)
            if state.follow_up_root_id is None:
                if state.unresolved_frontier:
                    now = self._observation(state)
                    candidate = replace(state, follow_up_root_id=self.follow_up_root(incident_id),
                                        updated_at=now, observed_at=now)
                    try:
                        self._d2.update(candidate, expected_revision=state.revision)
                    except RcaContinuationConcurrencyError:
                        pass
                    continue
                return FollowUpResult(
                    FollowUpDisposition.COMPLETE if self._events_covered(incident_id, state)
                    else FollowUpDisposition.OUTSTANDING, state)
            if state.follow_up_root_id != self.follow_up_root(incident_id):
                raise _Repair("parallel follow-up root")
            if (state.follow_up_wake_at is None and any(
                member.requirement_type == "POST_CONTEXT" for member in state.admitted_frontier
            )):
                # Older S4 records kept the B boundary reference but cleared
                # the scheduling wake at completion. Recover the original
                # absolute boundary from that public immutable B Snapshot.
                wakes = tuple(self._member_wake(incident_id, member)
                              for member in state.admitted_frontier
                              if member.requirement_type == "POST_CONTEXT")
                now = self._observation(state)
                candidate = replace(state, follow_up_wake_at=min(wakes),
                                    updated_at=now, observed_at=now)
                try:
                    self._d2.update(candidate, expected_revision=state.revision)
                except RcaContinuationConcurrencyError:
                    pass
                continue
            unresolved = []
            proofs = []
            previous = {proof.member: proof for proof in state.frontier_resolutions}
            invalidated = False
            try:
                for member in state.admitted_frontier:
                    old = previous.get(member)
                    supplied_snapshot = (post_context_snapshots or {}).get(member)
                    coverage_snapshot = (supplied_snapshot if supplied_snapshot is not None else
                                         old.coverage_snapshot_id if old is not None else None)
                    observed = self._coverage(incident_id, member, state,
                                              coverage_snapshot)
                    if old is not None and old.state != "INVALIDATED" and (
                        observed is None or replace(old, state="PROVISIONAL") != observed
                    ):
                        proofs.append(replace(old, state="INVALIDATED"))
                        unresolved.append(member)
                        invalidated = True
                    elif observed is None:
                        unresolved.append(member)
                        if old is not None:
                            proofs.append(replace(old, state="INVALIDATED"))
                    elif old is not None and old.state == "PROVISIONAL":
                        proofs.append(replace(observed, state="VALIDATED"))
                    elif old is not None and old.state == "VALIDATED":
                        proofs.append(old)
                    else:
                        proofs.append(observed)
                    if proofs and proofs[-1].member == member and proofs[-1].state != "VALIDATED" and member not in unresolved:
                        unresolved.append(member)
            except _CurrentChanged:
                continue
            complete = not unresolved and not invalidated
            same = (tuple(unresolved) == state.unresolved_frontier and
                    tuple(proofs) == state.frontier_resolutions and
                    complete == state.follow_up_complete)
            if same:
                # A/B are read again even after a terminal D2 record is reopened.
                if self._d2.get(root).revision != state.revision:
                    continue
                if complete and not self._events_covered(incident_id, state):
                    return FollowUpResult(FollowUpDisposition.OUTSTANDING, state)
                return FollowUpResult(FollowUpDisposition.COMPLETE if complete else
                                      FollowUpDisposition.OUTSTANDING, state)
            now = self._observation(state)
            due = state.next_eligibility_at
            if unresolved and all(item.requirement_type == "POST_CONTEXT"
                                  for item in unresolved):
                due = max(self._member_wake(incident_id, item) for item in unresolved)
            elif state.follow_up_wake_at is not None:
                due = state.follow_up_wake_at if due is None else min(due, state.follow_up_wake_at)
            candidate = replace(state, unresolved_frontier=tuple(unresolved),
                                frontier_resolutions=tuple(proofs),
                                follow_up_complete=complete,
                                next_eligibility_at=due if unresolved else None,
                                updated_at=now, observed_at=now)
            try:
                self._d2.resolve_follow_up(candidate, expected_revision=state.revision)
                # The first CAS stores provisional proof; a fresh iteration validates it.
                continue
            except RcaContinuationConcurrencyError:
                continue

    def refresh(self, incident_id: str, member: FollowUpRequirement, *,
                evidence_snapshot_id: str, knowledge_snapshot_id: str,
                lineage: AttemptLineage) -> FollowUpResult:
        """Direct B judgement, A STALE, B/C basis freeze, then A Attempt."""
        try:
            return self._refresh(incident_id, member, evidence_snapshot_id,
                                 knowledge_snapshot_id, lineage)
        except _Unavailable:
            return FollowUpResult(FollowUpDisposition.UNAVAILABLE)
        except OSError:
            return FollowUpResult(FollowUpDisposition.UNAVAILABLE)
        except Exception:
            return FollowUpResult(FollowUpDisposition.REPAIR_REQUIRED)

    def _refresh(self, incident_id: str, member: FollowUpRequirement,
                 evidence_snapshot_id: str, knowledge_snapshot_id: str,
                 lineage: AttemptLineage) -> FollowUpResult:
        revision_id = member.reference[len("revision:"):]
        while True:
            incident, aggregate, current = self._subject(incident_id)
            state = self._state(rca_root_id(incident_id), incident_id)
            if member.requirement_type != "MATERIAL_EVIDENCE" or member not in state.unresolved_frontier:
                raise _Repair("Evidence Revision was not durably admitted")
            if state.follow_up_root_id != self.follow_up_root(incident_id):
                raise _Repair("follow-up root is contradictory")
            if incident.status is IncidentStatus.CLOSED and (
                state.refresh_basis is None or state.refresh_basis.requirement != member
            ):
                raise _Repair("lifecycle disallows new automatic refresh")
            if current is None:
                raise _Repair("refresh requires authoritative Current baseline")
            try:
                judgement = self._pairwise(incident_id, current, revision_id)
                if (current.current.freshness is CurrentFreshness.STALE and
                    current.current.material_evidence_revision_basis == revision_id):
                    # A already persisted this trigger. SAME against the new Current basis
                    # does not undo the prior direct MATERIAL authorization on replay.
                    judgement = self._stale_trigger_materiality(incident_id, current, revision_id)
            except _CurrentChanged:
                continue
            if judgement.judgement in (MaterialityJudgement.SAME, MaterialityJudgement.NON_MATERIAL):
                checked = self._recheck(incident_id)
                if member in checked.continuation.unresolved_frontier:
                    return FollowUpResult(FollowUpDisposition.WAITING, checked.continuation, judgement)
                return FollowUpResult(FollowUpDisposition.SUPPRESSED, checked.continuation, judgement)
            if judgement.judgement is not MaterialityJudgement.MATERIAL:
                raise _Repair("B materiality is not a material authorization")
            if current.current.freshness is CurrentFreshness.FRESH:
                stale = CurrentRca(aggregate.aggregate_id, current.current.current_version_id,
                                   CurrentFreshness.STALE, revision_id)
                try:
                    self._a.apply_authorized_freshness(stale)
                except RcaDomainError:
                    changed = self._a.get_current(aggregate.aggregate_id)
                    if isinstance(changed, CurrentRcaRead) and changed.current != current.current:
                        continue
                    raise
            elif (current.current.freshness is CurrentFreshness.STALE and
                  current.current.material_evidence_revision_basis == revision_id):
                stale = current.current
            else:
                return FollowUpResult(FollowUpDisposition.WAITING, state, judgement)
            verified = self._a.get_current(aggregate.aggregate_id)
            if not isinstance(verified, CurrentRcaRead):
                raise _Repair("authorized STALE Current is unreadable")
            if verified.current != stale or verified.version.version_id != current.version.version_id:
                continue
            break
        stale_member = FollowUpRequirement("STALE_REFRESH", "version:" + stale.current_version_id)
        self._admit(incident_id, stale_member, scheduled_requirement=member)
        snapshot = self._b.resolve_snapshot(evidence_snapshot_id)
        revision = self._b.resolve_revision(revision_id)
        knowledge = self._c.read_snapshot(KnowledgeSnapshotKey(knowledge_snapshot_id))
        if knowledge.status is KnowledgeReadStatus.UNAVAILABLE:
            raise _Unavailable("C Knowledge Snapshot is unavailable")
        if (not isinstance(snapshot, EvidenceSnapshot) or not isinstance(revision, EvidenceRevision)
            or snapshot.snapshot_id != evidence_snapshot_id or snapshot.revision_id != revision_id
            or revision.incident_id != incident_id or snapshot.incident_id != incident_id
            or knowledge.status is not KnowledgeReadStatus.FOUND
            or knowledge.value.snapshot_key.value != knowledge_snapshot_id
            or knowledge.value.resolution not in (
                RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH,
                RetrievalResolution.RETRIEVAL_UNAVAILABLE)):
            raise _Repair("fresh Evidence or Knowledge basis is unavailable or contradictory")
        if (not isinstance(lineage, AttemptLineage) or lineage.aggregate_id != aggregate.aggregate_id
            or lineage.evidence_snapshot_id != evidence_snapshot_id
            or lineage.evidence_revision_id != revision_id
            or lineage.knowledge_snapshot_id != knowledge_snapshot_id):
            raise _Repair("proposed Attempt lineage contradicts frozen basis")
        operation = derive_attempt_admission_operation_id(rca_root_id(incident_id), lineage)
        basis = RefreshBasis(member, stale.current_version_id, evidence_snapshot_id,
                             revision_id, knowledge_snapshot_id,
                             judgement.materiality_result_id, lineage.attempt_id,
                             hashlib.sha256(operation.encode()).hexdigest())
        while True:
            state = self._state(rca_root_id(incident_id), incident_id)
            if state.refresh_basis is not None:
                if state.refresh_basis != basis:
                    if (state.refresh_basis.requirement in state.unresolved_frontier
                        or state.refresh_basis.baseline_version_id == basis.baseline_version_id):
                        return FollowUpResult(FollowUpDisposition.WAITING, state, judgement)
                    now = self._observation(state)
                    candidate = replace(state, refresh_basis=basis,
                                        updated_at=now, observed_at=now)
                    try:
                        state = self._d2.rotate_refresh_basis(candidate,
                            expected_revision=state.revision)
                        break
                    except RcaContinuationConcurrencyError:
                        continue
                break
            now = self._observation(state)
            candidate = replace(state, refresh_basis=basis, updated_at=now, observed_at=now)
            try:
                state = self._d2.update(candidate, expected_revision=state.revision)
                break
            except RcaContinuationConcurrencyError:
                continue
        frozen = self._state(rca_root_id(incident_id), incident_id)
        fresh_incident, _, _ = self._subject(incident_id)
        existing = self._a.get_attempt_lineage(lineage.attempt_id)
        if (fresh_incident.status is IncidentStatus.CLOSED and existing is None):
            raise _Repair("CLOSED Incident cannot admit a refresh Attempt")
        if (fresh_incident.status is IncidentStatus.AWAITING_REVIEW
            and frozen.refresh_basis != basis and existing is None):
            raise _Repair("lifecycle changed before Attempt admission")
        verified = self._a.get_current(aggregate.aggregate_id)
        if (frozen.refresh_basis != basis or not isinstance(verified, CurrentRcaRead)
            or verified.current != stale):
            raise _Repair("refresh basis or STALE Current changed before admission")
        if existing is None:
            admitted = self._a.admit_attempt(AdmitAttemptRequest(operation, lineage, self._clock.now()))
            if admitted.lineage != lineage:
                raise _Repair("A admitted a contradictory Attempt")
        elif existing.attempt.lineage != lineage:
            raise _Repair("A Attempt identity contradicts frozen lineage")
        return FollowUpResult(FollowUpDisposition.ATTEMPT_ADMITTED, frozen, judgement)

    def _events_covered(self, incident_id: str, state: RcaContinuation) -> bool:
        """Empty D2 continuity does not prove absence of domain obligations.

        Use B's immutable Event projections from the exact A Current basis
        or freshly revalidated POST_CONTEXT coverage proofs. Event identity
        coverage does not authorize Materiality, STALE or an Attempt.
        """
        incident, _, current = self._subject(incident_id)
        if current is None:
            raise _Repair("completion lacks authoritative A Current")
        lineage = current.attempt_lineage.attempt.lineage
        if (lineage.evidence_snapshot_id != current.artifact.provenance.evidence_snapshot_id
            or lineage.evidence_revision_id != current.artifact.provenance.evidence_revision_id):
            raise _Repair("A Current Evidence lineage contradicts Artifact")
        snapshot_ids = {lineage.evidence_snapshot_id}
        snapshot_ids.update(proof.coverage_snapshot_id for proof in state.frontier_resolutions
            if proof.member.requirement_type == "POST_CONTEXT" and proof.state == "VALIDATED"
            and proof.coverage_snapshot_id is not None)
        projections = []
        for snapshot_id in sorted(snapshot_ids):
            snapshot = self._b.resolve_snapshot(snapshot_id)
            if (not isinstance(snapshot, EvidenceSnapshot) or snapshot.snapshot_id != snapshot_id
                or snapshot.incident_id != incident_id
                or snapshot_id == lineage.evidence_snapshot_id
                and snapshot.revision_id != lineage.evidence_revision_id):
                raise _Repair("completion Evidence Snapshot is unreadable or contradictory")
            content = snapshot.snapshot_content
            captured = content.get("event_projections")
            subject = content.get("incident_projection")
            if (not isinstance(captured, list) or not captured
                or any(not isinstance(item, dict) or not isinstance(item.get("event_id"), str)
                       or not item["event_id"] for item in captured)
                or not isinstance(subject, dict) or subject.get("incident_id") != incident_id):
                raise _Repair("completion Evidence Event projections are malformed")
            ids = tuple(item["event_id"] for item in captured)
            subject_ids = subject.get("event_ids")
            if (len(set(ids)) != len(ids) or subject_ids is not None and (
                not isinstance(subject_ids, (tuple, list))
                or any(not isinstance(value, str) or not value for value in subject_ids)
                or len(set(subject_ids)) != len(subject_ids) or set(ids) != set(subject_ids))):
                raise _Repair("completion Evidence Incident and Event projections contradict")
            projections.append(set(ids))
        # A new correlation may commit during the B read. Re-read both
        # public authorities before accepting the coverage observation.
        incident, _, fresh = self._subject(incident_id)
        if (fresh is None or fresh.current != current.current
            or fresh.version.version_id != current.version.version_id):
            raise _CurrentChanged("A Current changed during completion coverage")
        events = getattr(incident, "event_ids", None)
        if (not isinstance(events, (tuple, list)) or not events
            or any(not isinstance(value, str) or not value for value in events)
            or len(set(events)) != len(events)):
            raise _Repair("completion Incident Event authority is malformed")
        return any(ids == set(events) for ids in projections)

    def _coverage(self, incident_id: str, member: FollowUpRequirement,
                  state: RcaContinuation, post_context_snapshot_id: str | None = None) -> FollowUpResolution | None:
        _, aggregate, current = self._subject(incident_id)
        if current is None:
            return None
        result = None
        snapshot_id = None
        if member.requirement_type == "MATERIAL_EVIDENCE":
            revision_id = member.reference[len("revision:"):]
            result = self._pairwise(incident_id, current, revision_id)
            if (current.current.freshness is CurrentFreshness.STALE and
                current.current.material_evidence_revision_basis == revision_id):
                self._stale_trigger_materiality(incident_id, current, revision_id)
                return None
            if result.judgement not in (MaterialityJudgement.SAME, MaterialityJudgement.NON_MATERIAL):
                return None
        elif member.requirement_type == "POST_CONTEXT":
            original = self._post_context_snapshot(incident_id, member)
            source = original if post_context_snapshot_id is None else self._b.resolve_snapshot(
                post_context_snapshot_id)
            if (not isinstance(source, EvidenceSnapshot)
                or source.snapshot_id != (post_context_snapshot_id or original.snapshot_id)
                or source.incident_id != incident_id):
                raise _Repair("B post-context coverage Snapshot is unreadable")
            facts = source.snapshot_content.get("post_context")
            if not isinstance(facts, dict):
                raise _Repair("B post-context coverage facts are malformed")
            original_boundary = self._snapshot_boundary(original)
            boundary = self._snapshot_boundary(source)
            if boundary < original_boundary:
                raise _Repair("B post-context coverage precedes the admitted horizon")
            incident, _, _ = self._subject(incident_id)
            current_events = getattr(incident, "event_ids", None)
            captured_events = source.snapshot_content.get("event_projections")
            if (not isinstance(current_events, (tuple, list)) or
                not isinstance(captured_events, list) or
                not all(isinstance(item, dict) and isinstance(item.get("event_id"), str)
                        for item in captured_events)):
                raise _Repair("authoritative post-context Event identity is unreadable")
            captured_ids = tuple(item["event_id"] for item in captured_events)
            if len(set(captured_ids)) != len(captured_ids):
                raise _Repair("B post-context Event identities are contradictory")
            if set(captured_ids) != set(current_events):
                return None
            episode = source.snapshot_content.get("episode")
            episode_end = None if not isinstance(episode, dict) else episode.get("end")
            reported_end = facts.get("episode_end")
            if (source.snapshot_content.get("incident_projection", {}).get("incident_id") != incident_id or
                not isinstance(episode_end, str) or not isinstance(reported_end, str) or
                canonical_utc(datetime.fromisoformat(episode_end.replace("Z", "+00:00")),
                              field="episode end") !=
                canonical_utc(datetime.fromisoformat(reported_end.replace("Z", "+00:00")),
                              field="post-context episode end")):
                raise _Repair("B post-context episode or Incident facts contradict")
            reached = facts["reached_upper_boundary"]
            if not isinstance(reached, dict) or set(reached) != {"LOKI", "PROMETHEUS"}:
                raise _Repair("B post-context facts are malformed")
            if any(value not in (True, False) or not isinstance(value, bool)
                   for value in reached.values()):
                raise _Repair("B post-context completion facts are malformed")
            observed = facts.get("snapshot_at")
            if not isinstance(observed, str) or canonical_utc(
                datetime.fromisoformat(observed.replace("Z", "+00:00")), field="snapshot_at"
            ) != source.snapshot_at:
                raise _Repair("B post-context snapshot time contradicts")
            windows = source.snapshot_content.get("windows")
            ends = facts.get("effective_window_ends")
            if (not isinstance(windows, dict) or not isinstance(ends, dict) or
                any(not isinstance(windows.get(name), dict) or
                    not isinstance(windows[name].get("end"), str) or
                    not isinstance(ends.get(name), str)
                    for name in ("LOKI", "PROMETHEUS"))):
                raise _Repair("B post-context effective windows contradict")
            for name in ("LOKI", "PROMETHEUS"):
                actual = canonical_utc(datetime.fromisoformat(
                    windows[name]["end"].replace("Z", "+00:00")), field="window end")
                reported = canonical_utc(datetime.fromisoformat(
                    ends[name].replace("Z", "+00:00")), field="effective end")
                if actual != reported or reached[name] != (reported == boundary):
                    raise _Repair("B post-context effective windows contradict")
            if not all(reached.values()):
                return None
            if source.snapshot_at < boundary:
                raise _Repair("B post-context completion precedes its boundary")
            snapshot_id = source.snapshot_id
        elif member.requirement_type == "STALE_REFRESH":
            baseline = member.reference[len("version:"):]
            if not (current.current.current_version_id != baseline
                    and current.current.freshness is CurrentFreshness.FRESH
                    and state.refresh_basis is not None
                    and current.attempt_lineage.attempt.lineage.attempt_id == state.refresh_basis.attempt_id
                    and current.attempt_lineage.attempt.lineage.evidence_revision_id == state.refresh_basis.evidence_revision_id):
                return None
        else:
            raise _Repair("unknown frontier type")
        self._require_same_current(current)
        return FollowUpResolution(
            member, current.current.current_version_id, current.current.freshness.value,
            current.current.material_evidence_revision_basis,
            current.attempt_lineage.attempt.lineage.attempt_id,
            current.artifact.provenance.evidence_revision_id,
            current.publication_result.target.publication_operation_id,
            None if result is None else result.request.baseline_revision_id,
            None if result is None else result.request.candidate_revision_id,
            None if result is None else result.request.materiality_rule_version,
            None if result is None else result.materiality_result_id,
            None if result is None else result.judgement.value,
            snapshot_id, "PROVISIONAL")

    def _pairwise(self, incident_id: str, current: CurrentRcaRead,
                  candidate_revision_id: str) -> MaterialityResult:
        attempt_revision = current.attempt_lineage.attempt.lineage.evidence_revision_id
        if current.artifact.provenance.evidence_revision_id != attempt_revision:
            raise _Repair("A Current Artifact and Attempt baseline contradict")
        baseline = current.current.material_evidence_revision_basis
        result = self._compare_pairwise(incident_id, baseline, candidate_revision_id)
        self._require_same_current(current)
        return result

    def _stale_trigger_materiality(self, incident_id: str, current: CurrentRcaRead,
                                   revision_id: str) -> MaterialityResult:
        lineage = self._a.get_freshness_lineage(current.current.aggregate_id)
        if (not isinstance(lineage, tuple) or len(lineage) < 2
            or lineage[-1] != current.current
            or lineage[-2].aggregate_id != current.current.aggregate_id
            or lineage[-2].current_version_id != current.current.current_version_id
            or lineage[-2].freshness is not CurrentFreshness.FRESH
            or lineage[-2].material_evidence_revision_basis == revision_id):
            raise _Repair("A STALE trigger has no authoritative prior FRESH basis")
        result = self._compare_pairwise(incident_id,
                                        lineage[-2].material_evidence_revision_basis, revision_id)
        self._require_same_current(current)
        if result.judgement is not MaterialityJudgement.MATERIAL:
            raise _Repair("A STALE trigger contradicts B direct MATERIAL judgement")
        return result

    def _compare_pairwise(self, incident_id: str, baseline: str,
                          candidate_revision_id: str) -> MaterialityResult:
        candidate = self._b.resolve_revision(candidate_revision_id)
        baseline_revision = self._b.resolve_revision(baseline)
        if (not isinstance(candidate, EvidenceRevision)
            or not isinstance(baseline_revision, EvidenceRevision)
            or candidate.incident_id != incident_id or baseline_revision.incident_id != incident_id):
            raise _Repair("B pairwise Revision authority is unreadable")
        request = MaterialityRequest(MaterialityEvaluationKind.PAIRWISE,
                                     candidate_revision_id, self._rule, baseline)
        result = self._b.compare_materiality(request)
        if not isinstance(result, MaterialityResult) or result.request != request:
            raise _Repair("B pairwise Materiality receipt is contradictory")
        if result.judgement is MaterialityJudgement.REPAIR_REQUIRED:
            raise _Repair("B pairwise Materiality requires repair")
        return result

    def _require_same_current(self, current: CurrentRcaRead) -> None:
        fresh = self._a.get_current(current.current.aggregate_id)
        if not isinstance(fresh, CurrentRcaRead):
            raise _Repair("A Current became unreadable during B comparison")
        if fresh.current != current.current or fresh.version.version_id != current.version.version_id:
            raise _CurrentChanged("A Current changed during B comparison")

    def _post_context_snapshot(self, incident_id: str,
                               member: FollowUpRequirement) -> EvidenceSnapshot:
        snapshot_id = member.reference[len("boundary:"):]
        snapshot = self._b.resolve_snapshot(snapshot_id)
        if (not isinstance(snapshot, EvidenceSnapshot) or snapshot.snapshot_id != snapshot_id
            or snapshot.incident_id != incident_id
            or not isinstance(snapshot.snapshot_content.get("post_context"), dict)):
            raise _Repair("B post-context Snapshot is unreadable or contradictory")
        return snapshot

    def _member_wake(self, incident_id: str, member: FollowUpRequirement) -> datetime | None:
        if member.requirement_type == "MATERIAL_EVIDENCE":
            revision_id = member.reference[len("revision:"):]
            revision = self._b.resolve_revision(revision_id)
            if (not isinstance(revision, EvidenceRevision) or revision.revision_id != revision_id
                or revision.incident_id != incident_id):
                raise _Repair("B Evidence Revision is unreadable")
            return None
        if member.requirement_type == "POST_CONTEXT":
            snapshot = self._post_context_snapshot(incident_id, member)
            return self._snapshot_boundary(snapshot)
        if member.requirement_type == "STALE_REFRESH":
            return None
        raise _Repair("unknown frontier type")

    @staticmethod
    def _snapshot_boundary(snapshot: EvidenceSnapshot) -> datetime:
        value = snapshot.snapshot_content["post_context"].get("default_boundary")
        if not isinstance(value, str) or not value.endswith("Z"):
            raise _Repair("B post-context boundary is not UTC")
        return canonical_utc(datetime.fromisoformat(value[:-1] + "+00:00"),
                             field="post-context boundary")

    def _require_stale_current(self, incident_id: str, version_id: str) -> None:
        _, _, current = self._subject(incident_id)
        if (not isinstance(current, CurrentRcaRead)
            or current.current.current_version_id != version_id
            or current.current.freshness is not CurrentFreshness.STALE):
            raise _Repair("STALE member lacks authoritative A Current")

    def _subject(self, incident_id: str) -> tuple[object, object, CurrentRcaRead | None]:
        subject = self._incidents.read_subject(incident_id)
        if subject.disposition is InitialRcaDisposition.UNAVAILABLE:
            raise _Unavailable("Incident public authority is unavailable")
        if subject.disposition is not InitialRcaDisposition.READY_FOR_TRY:
            raise _Repair("Incident public authority is absent or contradictory")
        self._a.validate_local_readiness()
        aggregate = self._a.get_aggregate_by_incident(incident_id)
        if aggregate is None or aggregate.incident_id != incident_id:
            raise _Repair("A Aggregate is absent or contradictory")
        current = self._a.get_current(aggregate.aggregate_id)
        if current is not None and not isinstance(current, CurrentRcaRead):
            raise _Repair("A Current is unreadable")
        if (current is not None and subject.relationship.current_version_id !=
            current.current.current_version_id):
            raise _Repair("Incident and A Current relationships contradict")
        return subject.incident, aggregate, current

    def _state(self, root: str, incident_id: str) -> RcaContinuation:
        enumeration = self._d2.enumerate_all()
        if enumeration.isolated_corruptions:
            raise _Repair("D2 continuation enumeration is incomplete")
        matches = tuple(item for item in enumeration.records if item.incident_id == incident_id)
        if len(matches) != 1 or matches[0].root_id != root:
            raise _Repair("Incident has parallel or absent RCA roots")
        state = self._d2.get(root)
        if state is None or state.incident_id != incident_id or state.root_id != root:
            raise _Repair("D2 RCA continuation is absent or contradictory")
        self._d2.get_follow_up_work(root)
        if state.runtime_work_id != runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root):
            raise _Repair("D2 RCA root work binding contradicts stable identity")
        aggregate = self._a.get_aggregate_by_incident(incident_id)
        if state.aggregate_id is not None and (
            aggregate is None or aggregate.aggregate_id != state.aggregate_id
        ):
            raise _Repair("D2 Aggregate reference contradicts A authority")
        if state.follow_up_root_id not in (None, self.follow_up_root(incident_id)):
            raise _Repair("parallel follow-up root")
        if state.follow_up_complete and state.unresolved_frontier:
            raise _Repair("completed follow-up has unresolved members")
        if state.unresolved_frontier and state.follow_up_root_id is None and (
            state.frontier_resolutions or state.follow_up_complete or
            state.admitted_frontier != state.unresolved_frontier
        ):
            raise _Repair("frontier without root is not a legacy unresolved record")
        stale_members = tuple(member for member in state.unresolved_frontier
                              if member.requirement_type == "STALE_REFRESH")
        if len(stale_members) > 1:
            raise _Repair("contradictory STALE frontier")
        return state

    def _observation(self, state: RcaContinuation) -> datetime:
        return max(self._clock.now(), state.updated_at, state.observed_at)
