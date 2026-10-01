"""RCA recovery and dispatch inside the existing SPEC-011 worker cycle.

The host reads public domain facts.  It owns neither a domain store nor a
second clock, loop, retry budget, or publication winner rule.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from types import SimpleNamespace
from typing import Callable, Protocol

from incident_evidence.contracts import EvidenceRecoveryFacts
from knowledge_index.contracts import KnowledgeLocalReadiness
from llm_generation.contracts import LocalReadStatus
from rca_persistence.contracts import RecoveryCandidateKind

from .clock import RuntimeClock
from .contracts import RuntimeWorkKind, RuntimeWorkStatus
from .rca_continuation import RcaContinuation, SqliteRcaContinuationStore, rca_root_id
from .telemetry import RuntimeTelemetry, RuntimeTelemetryEvent
from .worker import RuntimeCycleResult


class RcaRecoveryKind(str, Enum):
    PUBLICATION = "PUBLICATION"
    VALIDATED_D_RESULT = "VALIDATED_D_RESULT"
    RETRYABLE_TRY_FAILURE = "RETRYABLE_TRY_FAILURE"
    NON_RETRYABLE_TRY_FAILURE = "NON_RETRYABLE_TRY_FAILURE"
    REPAIR_REQUIRED_TRY_FAILURE = "REPAIR_REQUIRED_TRY_FAILURE"
    RETRY_BUDGET_EXHAUSTED = "RETRY_BUDGET_EXHAUSTED"
    RETRY_PENDING = "RETRY_PENDING"
    FOLLOW_UP_PENDING = "FOLLOW_UP_PENDING"
    POST_CONTEXT_PENDING = "POST_CONTEXT_PENDING"
    STALE_CURRENT = "STALE_CURRENT"
    INITIAL_INCIDENT = "INITIAL_INCIDENT"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    KNOWLEDGE_NOT_READY = "KNOWLEDGE_NOT_READY"


@dataclass(frozen=True, slots=True)
class RcaRecoverySubject:
    incident_id: str
    kind: RcaRecoveryKind
    operation_id: str | None = None
    work_id: str | None = None
    next_eligibility: datetime | None = None
    stage: str | None = None
    attempt_id: str | None = None


class RcaActions(Protocol):
    def reconcile_publication(self, operation_id: str, observed_at: datetime) -> object: ...
    def advance(self, subject: RcaRecoverySubject, observed_at: datetime) -> bool: ...


class RcaRuntimeHost:
    """Caller-driven RCA steps within one existing Runtime startup/worker."""

    def __init__(self, *, incidents: object, candidate_a: object,
                 candidate_b: object, candidate_c: object, candidate_d: object,
                 continuations: SqliteRcaContinuationStore, actions: RcaActions,
                 clock: RuntimeClock, telemetry: RuntimeTelemetry,
                 runtime_work_store: object | None = None) -> None:
        self._incidents, self._a, self._b = incidents, candidate_a, candidate_b
        self._c, self._d = candidate_c, candidate_d
        self._d2, self._actions = continuations, actions
        self._clock, self._telemetry = clock, telemetry
        self._runtime_work = runtime_work_store
        self._subjects: tuple[RcaRecoverySubject, ...] = ()
        self._classified = False

    @property
    def subjects(self) -> tuple[RcaRecoverySubject, ...]:
        return self._subjects

    def recover(self, runtime_snapshot: object) -> None:
        """Classify under the existing Startup Recovery Barrier.

        Publication is reconciled before any generation, retry, or refresh
        advancement.  An unreadable required authority leaves RCA closed.
        """
        self._classified = False
        self._subjects = ()
        try:
            self._incidents.validate_readiness()
            views = tuple(self._incidents.list_correlation_views())
            ids = tuple(item.incident_id for item in views)
            if len(ids) != len(set(ids)) or any(not isinstance(x, str) for x in ids):
                raise ValueError("Incident enumeration is contradictory")
            self._a.validate_local_readiness()
            a_candidates = tuple(self._a.enumerate_recovery_candidates())
            b_readiness = self._b.validate_local_readiness()
            b_facts = self._b.enumerate_recovery_facts()
            if not isinstance(b_facts, EvidenceRecoveryFacts):
                raise ValueError("Candidate-B enumeration is incomplete")
            c_readiness = self._c.local_readiness()
            d_readiness = self._d.local_readiness()
            d_recovery = self._d.recovery()
            d2 = self._d2.enumerate_all()
            if d2.isolated_corruptions:
                raise ValueError("D2 RCA enumeration is incomplete")
            if getattr(b_readiness, "value", b_readiness) not in ("READY", "READABLE"):
                raise ValueError("Candidate-B authority is not readable")
            if (c_readiness.status not in (KnowledgeLocalReadiness.READY,
                                           KnowledgeLocalReadiness.NOT_INITIALIZED) or
                d_readiness is not LocalReadStatus.FOUND or
                d_recovery.status is not LocalReadStatus.FOUND):
                raise ValueError("required C/D RCA authority is not readable")
            if (b_facts.local_readiness is not b_readiness or
                getattr(b_facts.integrity_status, "value", None) != "VALID" or
                b_facts.repair_findings):
                raise ValueError("Candidate-B recovery facts are incomplete")
            if self._runtime_work is not None:
                # The same D2 authority fences an earlier process before
                # reconciliation or dispatch. Change an RCA-specific action
                # token so a fixed Clock still advances the CAS revision;
                # preserve identities, retry slots and all absolute times.
                for state in d2.records:
                    active = self._d2.get_follow_up_work(state.root_id)
                    if active.status is RuntimeWorkStatus.OUTSTANDING:
                        self._runtime_work.update(replace(active,
                            next_action=f"RCA_RECOVERY_FENCE:{active.revision}"),
                            expected_revision=active.revision)
                runtime_snapshot = SimpleNamespace(
                    runtime_work=self._runtime_work.enumerate_all())
            subjects = self._classify(ids, a_candidates, b_facts, d_recovery.value,
                                      d2.records, runtime_snapshot)
            publications = tuple(item for item in subjects
                                 if item.kind is RcaRecoveryKind.PUBLICATION)
            for item in publications:
                outcome = self._actions.reconcile_publication(item.operation_id, self._clock.now())
                self._telemetry.emit(RuntimeTelemetryEvent.RECONCILIATION,
                    observed_at=self._clock.now(), incident_id=item.incident_id,
                    root_id=rca_root_id(item.incident_id),
                    stage="RCA_PUBLICATION_STARTUP", operation_id=item.operation_id,
                    publication_classification=getattr(
                        getattr(outcome, "classification", None), "value", None))
            if publications:
                # Reconciliation can complete a Work or admit a follow-up.
                # Never dispatch generation from the pre-reconciliation view.
                refreshed_b = self._b.enumerate_recovery_facts()
                refreshed_d = self._d.recovery()
                refreshed_d2 = self._d2.enumerate_all()
                refreshed_work = (self._runtime_work.enumerate_all()
                    if self._runtime_work is not None else runtime_snapshot.runtime_work)
                if (not isinstance(refreshed_b, EvidenceRecoveryFacts) or
                    refreshed_b.integrity_status.value != "VALID" or
                    refreshed_b.repair_findings or
                    refreshed_d.status is not LocalReadStatus.FOUND or
                    refreshed_d2.isolated_corruptions):
                    raise ValueError("post-publication recovery enumeration is incomplete")
                subjects = self._classify(ids,
                    tuple(self._a.enumerate_recovery_candidates()), refreshed_b,
                    refreshed_d.value, refreshed_d2.records,
                    SimpleNamespace(runtime_work=refreshed_work))
            if c_readiness.status is KnowledgeLocalReadiness.NOT_INITIALIZED:
                # C's reliable absence only gates an otherwise clean initial
                # obligation. Preserve every typed D2/A/B/D recovery finding,
                # especially irreconstructible continuity and repair states.
                other_facts = {item.incident_id for item in subjects
                    if item.kind not in (RcaRecoveryKind.INITIAL_INCIDENT,
                                         RcaRecoveryKind.PUBLICATION)}
                self._subjects = tuple(
                    replace(item, kind=RcaRecoveryKind.KNOWLEDGE_NOT_READY)
                    if (item.kind is RcaRecoveryKind.INITIAL_INCIDENT and
                        item.incident_id not in other_facts) else item
                    for item in subjects)
            else:
                self._subjects = subjects
            for item in self._subjects:
                state = (self._d2.get(rca_root_id(item.incident_id))
                    if item.incident_id != "RCA-CAPABILITY" else None)
                work = (self._runtime_work.get(item.work_id)
                    if self._runtime_work is not None and item.work_id else None)
                self._telemetry.emit(RuntimeTelemetryEvent.RECOVERY,
                    observed_at=self._clock.now(), incident_id=item.incident_id,
                    root_id=(rca_root_id(item.incident_id)
                             if item.incident_id != "RCA-CAPABILITY" else None),
                    stage=item.stage or "RCA_STARTUP",
                    recovery_classification=item.kind.value,
                    work_kind=(work.work_kind if work is not None else None),
                    attempt_id=item.attempt_id,
                    retry_lane=(work.stage if work is not None else None),
                    consumed_slots=(work.attempt_count if work is not None else None),
                    frontier_count=(len(state.unresolved_frontier)
                                    if state is not None else None),
                    wake_at=item.next_eligibility,
                    exhaustion=item.kind is RcaRecoveryKind.RETRY_BUDGET_EXHAUSTED)
            self._classified = True
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self._telemetry.emit(RuntimeTelemetryEvent.RECOVERY,
                observed_at=self._clock.now(), stage="RCA_REPAIR_REQUIRED",
                reconciliation_result="REPAIR_REQUIRED")
            # RCA authority is closed for this process; the existing
            # SPEC-011 barrier still decides whole-platform readiness.
            self._subjects = (RcaRecoverySubject("RCA-CAPABILITY",
                RcaRecoveryKind.REPAIR_REQUIRED),)
            self._classified = True

    def _classify(self, incident_ids: tuple[str, ...], a_candidates: tuple[object, ...],
                  b_facts: EvidenceRecoveryFacts, d_recovery: object,
                  continuations: tuple[RcaContinuation, ...],
                  runtime_snapshot: object) -> tuple[RcaRecoverySubject, ...]:
        result: list[RcaRecoverySubject] = []
        blocked_incidents: set[str] = set()
        by_incident = {item.incident_id: item for item in continuations}
        if len(by_incident) != len(continuations):
            raise ValueError("parallel RCA continuation roots")
        runtime_records = runtime_snapshot.runtime_work.records
        by_work = {item.work_id: item for item in runtime_records}
        if runtime_snapshot.runtime_work.isolated_corruptions:
            raise ValueError("Runtime Work enumeration is incomplete")
        for incident_id in incident_ids:
            incident = self._incidents.get_incident(incident_id)
            if incident is None or incident.incident_id != incident_id:
                raise ValueError("Incident enumeration contradicts public read")
            state = by_incident.get(incident_id)
            aggregate = self._a.get_aggregate_by_incident(incident_id)
            if state is None:
                # A durable Aggregate may precede the first B effect. Once B
                # or A has a later effect, the missing frozen Capture Basis
                # cannot be recreated from wall-clock time.
                has_b_effect = any(item.incident_id == incident_id
                                   for item in b_facts.snapshots)
                has_a_effect = aggregate is not None and (
                    self._a.get_current(aggregate.aggregate_id) is not None or
                    bool(self._a.get_version_history(aggregate.aggregate_id)) or
                    any(item.aggregate_id == aggregate.aggregate_id
                        for item in a_candidates))
                if not has_b_effect and not has_a_effect:
                    result.append(RcaRecoverySubject(incident_id, RcaRecoveryKind.INITIAL_INCIDENT))
                else:
                    result.append(RcaRecoverySubject(incident_id, RcaRecoveryKind.REPAIR_REQUIRED))
                    blocked_incidents.add(incident_id)
                continue
            if state.root_id != rca_root_id(incident_id):
                raise ValueError("RCA root identity contradicts Incident")
            active = self._d2.get_follow_up_work(state.root_id)
            if active.work_id not in by_work or by_work[active.work_id] != active:
                raise ValueError("RCA work continuity contradicts D2 enumeration")
            durable_publication = False
            if (aggregate is not None and (state.stage == "EXECUTION" or
                (state.stage == "PUBLICATION" and not state.unresolved_frontier and
                 len(state.follow_up_capture_bases) == len(state.follow_up_capture_snapshots)))
                and active.status is RuntimeWorkStatus.OUTSTANDING):
                attempt_id = (state.refresh_basis.attempt_id if state.refresh_basis is not None
                              else state.attempt_id)
                versions = tuple(version for version in self._a.get_version_history(aggregate.aggregate_id)
                    if (version.attempt_id == attempt_id if attempt_id is not None else
                        state.publication_operation_id is not None and
                        version.publication_operation_id == state.publication_operation_id))
                if len(versions) > 1:
                    raise ValueError("RCA publication handoff is ambiguous")
                if versions:
                    version = versions[0]
                    receipt = self._a.get_publication_result(version.publication_operation_id)
                    if (receipt is None or receipt.target.incident_id != incident_id
                        or receipt.target.aggregate_id != aggregate.aggregate_id
                        or receipt.target.target_version_id != version.version_id
                        or (state.refresh_basis is None and
                            state.publication_operation_id not in (None, version.publication_operation_id))):
                        raise ValueError("RCA publication handoff contradicts public A intent")
                    durable_publication = True
                    # Completed A/008 receipts are absent from A's outstanding
                    # candidates. They still recover interrupted E bookkeeping.
                    if not any(candidate.publication_operation_id == version.publication_operation_id
                        for candidate in a_candidates if candidate.kind in (
                            RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION,
                            RecoveryCandidateKind.UNRESOLVED_PUBLICATION)):
                        result.append(RcaRecoverySubject(incident_id, RcaRecoveryKind.PUBLICATION,
                            version.publication_operation_id))
            if len(getattr(state, "follow_up_capture_bases", ())) > len(
                getattr(state, "follow_up_capture_snapshots", ())):
                result.append(RcaRecoverySubject(incident_id, RcaRecoveryKind.FOLLOW_UP_PENDING,
                    work_id=active.work_id, stage="CAPTURE"))
            if state.unresolved_frontier:
                result.append(RcaRecoverySubject(incident_id, RcaRecoveryKind.FOLLOW_UP_PENDING,
                    work_id=active.work_id, next_eligibility=state.next_eligibility_at,
                    stage=state.stage))
                if any(member.requirement_type == "POST_CONTEXT"
                       for member in state.unresolved_frontier):
                    result.append(RcaRecoverySubject(incident_id, RcaRecoveryKind.POST_CONTEXT_PENDING,
                        work_id=active.work_id, next_eligibility=state.next_eligibility_at))
            elif active.status is RuntimeWorkStatus.OUTSTANDING and not durable_publication:
                result.append(RcaRecoverySubject(incident_id, RcaRecoveryKind.RETRY_PENDING
                    if active.work_kind is RuntimeWorkKind.RCA_ATTEMPT else
                    RcaRecoveryKind.INITIAL_INCIDENT,
                    work_id=active.work_id, next_eligibility=active.next_retry_at,
                    stage=state.stage))
            if aggregate is not None:
                current = self._a.get_current(aggregate.aggregate_id)
                if current is not None:
                    lineage = self._a.get_attempt_lineage(
                        current.version.attempt_id)
                    if lineage is None:
                        raise ValueError("Current Attempt lineage is unreadable")
                    evidence_id = lineage.attempt.lineage.evidence_snapshot_id
                    evidence = next((item for item in b_facts.snapshots
                                     if item.snapshot_id == evidence_id), None)
                    if evidence is None or evidence.incident_id != incident_id:
                        raise ValueError("Current Evidence basis is unreadable")
                    events = evidence.snapshot_content.get("event_projections")
                    if not isinstance(events, list) or any(
                        not isinstance(item, dict) or
                        not isinstance(item.get("event_id"), str)
                        for item in events
                    ):
                        raise ValueError("Current Evidence Event set is malformed")
                    ids = tuple(item["event_id"] for item in events)
                    if len(set(ids)) != len(ids):
                        raise ValueError("Current Evidence Event set is contradictory")
                    if set(ids) != set(incident.event_ids):
                        result.append(RcaRecoverySubject(
                            incident_id, RcaRecoveryKind.FOLLOW_UP_PENDING,
                            work_id=active.work_id, stage="NEW_EVIDENCE"))
                    post = evidence.snapshot_content.get("post_context")
                    if not isinstance(post, dict):
                        raise ValueError("Current B boundary is unreadable")
                    reached = post.get("reached_upper_boundary")
                    if (not isinstance(reached, dict) or
                        set(reached) != {"LOKI", "PROMETHEUS"} or
                        any(type(value) is not bool for value in reached.values())):
                        raise ValueError("Current B source boundary is contradictory")
                    boundary = post.get("default_boundary")
                    if not isinstance(boundary, str):
                        raise ValueError("Current B boundary is malformed")
                    try:
                        boundary_at = datetime.fromisoformat(
                            boundary.replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise ValueError("Current B boundary is malformed") from exc
                    member_ref = "boundary:" + evidence.snapshot_id
                    if (evidence.snapshot_at < boundary_at or
                        not all(reached.values())) and not any(
                            item.requirement_type == "POST_CONTEXT" and
                            item.reference == member_ref
                            for item in state.admitted_frontier):
                        result.append(RcaRecoverySubject(
                            incident_id, RcaRecoveryKind.FOLLOW_UP_PENDING,
                            work_id=active.work_id, stage="POST_CONTEXT_DISCOVERY"))
        for work in runtime_records:
            if (work.work_kind is RuntimeWorkKind.RCA_ATTEMPT and
                work.status in (RuntimeWorkStatus.OUTSTANDING, RuntimeWorkStatus.EXHAUSTED)):
                if work.incident_id not in by_incident:
                    raise ValueError("RCA Attempt budget lacks root continuation")
                if work.status is RuntimeWorkStatus.EXHAUSTED and (
                    work.stage != "EXHAUSTED" or work.source_retry_disposition != "RETRYABLE"
                    or not any(item.try_identity.attempt_id == work.operation_id and
                               item.retry_safety.value == "RETRYABLE"
                               for item in d_recovery.failures)):
                    raise ValueError("RCA exhaustion lacks typed Candidate-D failure")
                result.append(RcaRecoverySubject(work.incident_id,
                    RcaRecoveryKind.RETRY_BUDGET_EXHAUSTED if work.status is RuntimeWorkStatus.EXHAUSTED
                    else RcaRecoveryKind.RETRY_PENDING, work_id=work.work_id,
                    next_eligibility=work.next_retry_at, stage=work.stage,
                    attempt_id=work.operation_id))
        for candidate in a_candidates:
            aggregate = self._a.get_aggregate(candidate.aggregate_id)
            if aggregate is None:
                raise ValueError("A recovery candidate lacks Aggregate")
            if candidate.kind in (RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION,
                                  RecoveryCandidateKind.UNRESOLVED_PUBLICATION):
                result.append(RcaRecoverySubject(aggregate.incident_id,
                    RcaRecoveryKind.PUBLICATION, candidate.publication_operation_id))
            elif candidate.kind is RecoveryCandidateKind.STALE_CURRENT:
                result.append(RcaRecoverySubject(aggregate.incident_id,
                    RcaRecoveryKind.STALE_CURRENT))
            elif candidate.kind is RecoveryCandidateKind.ATTEMPT_TRY_RECONCILIATION:
                result.append(RcaRecoverySubject(aggregate.incident_id,
                    RcaRecoveryKind.RETRY_PENDING, attempt_id=candidate.attempt_id))
        for fact in d_recovery.results:
            view = self._a.get_attempt_lineage(fact.try_identity.attempt_id)
            if view is None:
                raise ValueError("D result lacks A Attempt")
            aggregate = self._a.get_aggregate(view.attempt.lineage.aggregate_id)
            if aggregate is None:
                raise ValueError("D result lacks A Aggregate")
            matching = tuple(item for item in view.try_outcomes
                             if item.identity == fact.try_identity)
            if matching and any(item.validated_result_id != fact.validated_result_id
                                for item in matching):
                raise ValueError("D result contradicts A Try outcome")
            versions = self._a.get_version_history(aggregate.aggregate_id)
            if any(item.attempt_id == fact.try_identity.attempt_id for item in versions):
                continue
            result.append(RcaRecoverySubject(aggregate.incident_id,
                RcaRecoveryKind.VALIDATED_D_RESULT, fact.operation_id,
                attempt_id=fact.try_identity.attempt_id))
        for fact in d_recovery.failures:
            view = self._a.get_attempt_lineage(fact.try_identity.attempt_id)
            if view is None:
                raise ValueError("D failure lacks A Attempt")
            if any(item.identity == fact.try_identity for item in view.try_outcomes):
                continue
            aggregate = self._a.get_aggregate(view.attempt.lineage.aggregate_id)
            disposition = getattr(fact.retry_safety, "value", fact.retry_safety)
            kind = {"RETRYABLE": RcaRecoveryKind.RETRYABLE_TRY_FAILURE,
                    "NON_RETRYABLE": RcaRecoveryKind.NON_RETRYABLE_TRY_FAILURE,
                    "REPAIR_REQUIRED": RcaRecoveryKind.REPAIR_REQUIRED_TRY_FAILURE}.get(disposition)
            if kind is None:
                raise ValueError("D failure disposition is not typed")
            result.append(RcaRecoverySubject(aggregate.incident_id, kind, fact.operation_id,
                                             attempt_id=fact.try_identity.attempt_id))
        result = [item for item in result if item.incident_id not in blocked_incidents
                  or item.kind in (RcaRecoveryKind.REPAIR_REQUIRED,
                                   RcaRecoveryKind.PUBLICATION)]
        return tuple(sorted(result, key=lambda item: (item.incident_id, item.kind.value,
                                                    item.operation_id or "")))

    def run_cycle(self, *, should_stop: Callable[[], bool]) -> RuntimeCycleResult:
        if not self._classified:
            raise RuntimeError("RCA dispatch cannot cross startup recovery barrier")
        if self._runtime_work is not None:
            # A fresh public enumeration observes work admitted since startup.
            self.recover(SimpleNamespace(runtime_work=self._runtime_work.enumerate_all()))
        acquired = completed = 0
        next_at: datetime | None = None
        for item in self._subjects:
            if should_stop():
                break
            if item.kind in (RcaRecoveryKind.PUBLICATION, RcaRecoveryKind.REPAIR_REQUIRED,
                             RcaRecoveryKind.REPAIR_REQUIRED_TRY_FAILURE,
                             RcaRecoveryKind.POST_CONTEXT_PENDING,
                             RcaRecoveryKind.KNOWLEDGE_NOT_READY):
                continue
            if item.next_eligibility is not None and self._clock.now() < item.next_eligibility:
                next_at = item.next_eligibility if next_at is None else min(next_at, item.next_eligibility)
                continue
            try:
                outcome = self._actions.advance(item, self._clock.now())
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                self._subjects = (RcaRecoverySubject(item.incident_id,
                    RcaRecoveryKind.REPAIR_REQUIRED, work_id=item.work_id),)
                self._telemetry.emit(RuntimeTelemetryEvent.RECOVERY,
                    observed_at=self._clock.now(), incident_id=item.incident_id,
                    root_id=rca_root_id(item.incident_id),
                    stage="RCA_DISPATCH", action="FAIL_CLOSED",
                    recovery_classification="REPAIR_REQUIRED")
                break
            if outcome is True:
                acquired += 1
                completed += 1
            self._telemetry.emit(RuntimeTelemetryEvent.WORK_UPDATED,
                observed_at=self._clock.now(), incident_id=item.incident_id,
                root_id=rca_root_id(item.incident_id),
                stage="RCA_" + item.kind.value, operation_id=item.operation_id,
                action="ADVANCE", attempt_id=item.attempt_id,
                completion=outcome is True)
        return RuntimeCycleResult(acquired, completed, next_at)
