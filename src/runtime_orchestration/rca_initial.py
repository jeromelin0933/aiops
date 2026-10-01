"""SPEC-016 S2 initial RCA composition over existing public semantic ports.

This module deliberately owns neither an upstream store nor domain business
truth.  It is called by the existing Runtime and stops as soon as Candidate A
has admitted an immutable Attempt; Try execution belongs to a later slice.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Callable, Protocol

from incident_evidence.contracts import (CaptureCommand, CaptureTerminalKind, CaptureTerminalOutcome,
    MaterialityEvaluationKind, MaterialityRequest, MaterialityResult, NonTerminalInvocationFailure)
from incident_evidence.identity import capture_command_semantic_identity
from incident_management.contracts import IncidentRcaRelationship, IncidentRecord
from knowledge_index.contracts import (
    DurableRetrievalCompletion, KnowledgeReadStatus, KnowledgeResolutionOutcome,
    RetrievalOperationState, RetrievalResolution,
)
from rca_persistence.contracts import (
    AdmitAttemptRequest, AttemptLineage, CreateAggregateRequest, GenerationAttempt, GenerationProvenance,
    RcaAggregate, RcaDomainError, RecoveryCandidateKind,
)

from .rca_continuation import (
    CaptureCommandBasis, RcaContinuation, RcaContinuationConcurrencyError, RcaRepairRequiredError,
    SqliteRcaContinuationStore, derive_capture_operation_id, rca_child_operation_id,
    rca_root_id,
)
from .contracts import RuntimeWorkKind, RuntimeWorkRecord, RuntimeWorkStatus
from .identity import runtime_work_id
from .sqlite_work_store import (ContradictoryRuntimeWorkError, SqliteRuntimeWorkStore,
                                RuntimeWorkStoreError)


class InitialRcaDisposition(str, Enum):
    READY_FOR_TRY = "READY_FOR_TRY"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    EVIDENCE_FAILED = "EVIDENCE_FAILED"
    KNOWLEDGE_UNAVAILABLE = "KNOWLEDGE_UNAVAILABLE"


class InitialRcaError(RuntimeError):
    """A non-absence S2 composition failure; callers must fail closed."""


class InitialRcaUnavailable(InitialRcaError):
    pass


class InitialRcaRepairRequired(InitialRcaError):
    pass


class IncidentPublicReadPort(Protocol):
    """The minimal consumer-owned view of existing SPEC-008 public reads."""
    def list_correlation_views(self) -> tuple[object, ...]: ...
    def get_incident(self, incident_id: str) -> IncidentRecord | None: ...
    def get_rca_relationship(self, incident_id: str) -> IncidentRcaRelationship | None: ...
    def validate_readiness(self) -> None: ...


class CandidateBPublicPort(Protocol):
    """Existing EvidenceCaptureService semantics, never its SQLite store."""
    def read_capture_outcome(self, capture_operation_id: str) -> CaptureTerminalOutcome | None: ...
    def capture_evidence(self, command: CaptureCommand) -> CaptureTerminalOutcome | NonTerminalInvocationFailure: ...
    def resolve_snapshot(self, snapshot_id: str) -> object | None: ...
    def resolve_revision(self, revision_id: str) -> object | None: ...
    def compare_materiality(self, request: MaterialityRequest) -> MaterialityResult: ...


class CandidateAPublicPort(Protocol):
    def validate_local_readiness(self) -> None: ...
    def get_aggregate_by_incident(self, incident_id: str) -> RcaAggregate | None: ...
    def get_current(self, aggregate_id: str) -> object | None: ...
    def enumerate_recovery_candidates(self) -> tuple[object, ...]: ...
    def get_attempt_lineage(self, attempt_id: str) -> object | None: ...
    def create_or_discover_aggregate(self, request: CreateAggregateRequest) -> RcaAggregate: ...
    def admit_attempt(self, request: AdmitAttemptRequest) -> GenerationAttempt: ...


class CandidateCPublicPort(Protocol):
    def read_operation(self, key: object) -> object: ...
    def read_snapshot(self, key: object) -> object: ...
    def resolve(self, request: object, limits: object) -> KnowledgeResolutionOutcome: ...
    def finalize_unavailable(self, request: object) -> KnowledgeResolutionOutcome: ...


@dataclass(frozen=True, slots=True)
class IncidentSubjectRead:
    disposition: InitialRcaDisposition
    incident: IncidentRecord | None = None
    relationship: IncidentRcaRelationship | None = None


class IncidentPublicAdapter:
    """Reliable Incident absence requires enumeration, integrity, and fresh reads."""
    def __init__(self, authority: IncidentPublicReadPort) -> None:
        self._authority = authority

    def read_subject(self, incident_id: str) -> IncidentSubjectRead:
        try:
            self._authority.validate_readiness()
            views = tuple(self._authority.list_correlation_views())
            view_ids = tuple(getattr(view, "incident_id", None) for view in views)
            if any(not isinstance(value, str) for value in view_ids) or len(set(view_ids)) != len(view_ids):
                return IncidentSubjectRead(InitialRcaDisposition.REPAIR_REQUIRED)
            incident = self._authority.get_incident(incident_id)
            relationship = self._authority.get_rca_relationship(incident_id)
        except OSError:
            return IncidentSubjectRead(InitialRcaDisposition.UNAVAILABLE)
        except Exception:
            return IncidentSubjectRead(InitialRcaDisposition.REPAIR_REQUIRED)
        listed = incident_id in view_ids
        if incident is not None and incident.incident_id != incident_id:
            return IncidentSubjectRead(InitialRcaDisposition.REPAIR_REQUIRED)
        if relationship is not None and relationship.incident_id != incident_id:
            return IncidentSubjectRead(InitialRcaDisposition.REPAIR_REQUIRED)
        if listed != (incident is not None):
            return IncidentSubjectRead(InitialRcaDisposition.REPAIR_REQUIRED)
        if incident is None:
            if relationship is not None:
                return IncidentSubjectRead(InitialRcaDisposition.REPAIR_REQUIRED)
            return IncidentSubjectRead(InitialRcaDisposition.NOT_FOUND)
        if relationship is None:
            return IncidentSubjectRead(InitialRcaDisposition.REPAIR_REQUIRED)
        return IncidentSubjectRead(InitialRcaDisposition.READY_FOR_TRY, incident, relationship)


@dataclass(frozen=True, slots=True)
class InitialRcaRequest:
    incident_id: str
    runtime_work_id: str
    retry_limit: int
    capture_basis: CaptureCommandBasis
    authoritative_now: datetime
    capture_command: CaptureCommand
    retrieval_request: object
    retrieval_limits: object
    attempt_lineage: AttemptLineage | None
    materiality_request: MaterialityRequest | None
    unavailable_finalization_request: object | None = None
    attempt_id: str | None = None
    generation_provenance: GenerationProvenance | None = None
    materiality_rule_version: str = "evidence-materiality-v1"

    def __post_init__(self) -> None:
        root = rca_root_id(self.incident_id)
        if self.capture_basis.root_id != root or self.capture_basis.incident_id != self.incident_id:
            raise ValueError("Capture Command Basis contradicts Incident RCA root")
        if self.capture_basis.bounds_policy_version is None:
            raise ValueError("Capture Command Basis lacks bounds_policy_version")
        if self.capture_command.incident_id != self.incident_id:
            raise ValueError("Capture Command contradicts Incident")
        if self.capture_command.capture_operation_id != derive_capture_operation_id(self.capture_basis):
            raise ValueError("Capture Command must use the frozen basis identity")
        expected_capture = (
            self.capture_basis.snapshot_at, self.capture_basis.capture_contract_version,
            self.capture_basis.canonicalization_version, self.capture_basis.source_policy_version,
            self.capture_basis.bounds_policy_version, self.capture_basis.configuration_identity,
        )
        actual_capture = (
            self.capture_command.snapshot_at, self.capture_command.capture_contract_version,
            self.capture_command.canonicalization_version, self.capture_command.source_policy_version,
            self.capture_command.bounds_policy_version, self.capture_command.config_identity,
        )
        if actual_capture != expected_capture:
            raise ValueError("Capture Command contradicts frozen Capture Command Basis")
        attempt_id = self.attempt_lineage.attempt_id if self.attempt_lineage is not None else self.attempt_id
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("Initial RCA requires a stable Attempt identity")
        if self.attempt_lineage is None and not isinstance(self.generation_provenance, GenerationProvenance):
            raise ValueError("Initial RCA requires Candidate-A generation provenance")
        operation_key = getattr(self.retrieval_request, "operation_key", None)
        operation_id = getattr(operation_key, "value", None)
        expected_retrieval = rca_child_operation_id(root, self.capture_basis.capture_operation_id, "KNOWLEDGE_RETRIEVAL")
        if operation_id != expected_retrieval:
            raise ValueError("Retrieval operation must use the frozen Capture Command Basis identity")
        materiality = self.materiality_request
        if materiality is not None and (materiality.evaluation_kind is not MaterialityEvaluationKind.NO_BASELINE
            or materiality.baseline_revision_id is not None):
            raise ValueError("Initial RCA requires explicit Candidate-B NO_BASELINE Materiality")


@dataclass(frozen=True, slots=True)
class InitialRcaResult:
    disposition: InitialRcaDisposition
    continuation: RcaContinuation | None = None
    aggregate: RcaAggregate | None = None
    evidence: CaptureTerminalOutcome | None = None
    knowledge: KnowledgeResolutionOutcome | None = None
    attempt: GenerationAttempt | None = None
    materiality: MaterialityResult | None = None


def derive_attempt_admission_operation_id(root: str, lineage: AttemptLineage) -> str:
    """Bind A admission to the exact existing immutable lineage fields."""
    if not isinstance(lineage, AttemptLineage):
        raise TypeError("lineage must be an AttemptLineage")
    provenance = lineage.generation_provenance
    basis = (
        lineage.aggregate_id, lineage.evidence_snapshot_id, lineage.evidence_revision_id,
        lineage.knowledge_snapshot_id, provenance.provider_id, provenance.model_id,
        provenance.prompt_id, provenance.configuration_id, provenance.credential_profile_id,
        lineage.attempt_id,
    )
    subject = hashlib.sha256(json.dumps(basis, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()
    return rca_child_operation_id(root, subject, "ATTEMPT_ADMISSION")


class InitialRcaOrchestrator:
    """S2 ordering: Incident → A aggregate → B → C → A Attempt, then stop."""
    def __init__(self, *, incidents: IncidentPublicReadPort, candidate_a: CandidateAPublicPort,
                 candidate_b: CandidateBPublicPort, candidate_c: CandidateCPublicPort,
                 continuations: SqliteRcaContinuationStore) -> None:
        self._incidents = IncidentPublicAdapter(incidents)
        self._candidate_a = candidate_a
        self._candidate_b = candidate_b
        self._candidate_c = candidate_c
        self._continuations = continuations

    def run(self, request: InitialRcaRequest) -> InitialRcaResult:
        subject = self._incidents.read_subject(request.incident_id)
        if subject.disposition is not InitialRcaDisposition.READY_FOR_TRY:
            return InitialRcaResult(subject.disposition)
        root = rca_root_id(request.incident_id)
        continuation = None
        try:
            self._authorize_work(request, subject.incident, subject.relationship, root)
            continuation = self._load_or_create(root, request)
            aggregate = self._aggregate(root, request)
            continuation = self._advance(continuation, aggregate_id=aggregate.aggregate_id,
                                         stage="CAPTURE", next_action="FREEZE_CAPTURE_BASIS")
            continuation = self._freeze_capture_basis(continuation, request)
            evidence = self._evidence(request, continuation)
            if evidence is None:
                return InitialRcaResult(InitialRcaDisposition.UNAVAILABLE, continuation, aggregate)
            if evidence.terminal_kind is not CaptureTerminalKind.SUCCESS:
                return InitialRcaResult(InitialRcaDisposition.EVIDENCE_FAILED, continuation, aggregate, evidence)
            materiality_request = request.materiality_request or MaterialityRequest(
                MaterialityEvaluationKind.NO_BASELINE, evidence.revision_id, request.materiality_rule_version)
            if materiality_request.candidate_revision_id != evidence.revision_id:
                raise InitialRcaRepairRequired("Candidate-B Materiality request contradicts captured Revision")
            materiality = self._candidate_b.compare_materiality(materiality_request)
            if (materiality.request != materiality_request
                or materiality.request.evaluation_kind is not MaterialityEvaluationKind.NO_BASELINE
                or materiality.request.candidate_revision_id != evidence.revision_id
                or materiality.judgement is not None):
                raise InitialRcaRepairRequired("Candidate-B initial Materiality authority is contradictory")
            continuation = self._advance(continuation, materiality_result_id=materiality.materiality_result_id,
                                         stage="RETRIEVAL", next_action="RETRIEVAL")
            retrieval_id = rca_child_operation_id(root, continuation.capture_operation_id, "KNOWLEDGE_RETRIEVAL")
            # Once D2 has fixed an Attempt identity, restart must reuse it even
            # if the new process supplied a replacement local proposal.
            attempt_id = continuation.attempt_id or (request.attempt_lineage.attempt_id if request.attempt_lineage else request.attempt_id)
            continuation = self._advance(continuation, retrieval_operation_id=retrieval_id,
                                         attempt_id=attempt_id, stage="RETRIEVAL", next_action="RETRIEVAL")
            # A process may die immediately after this CAS. Read durable D2 and
            # Candidate-C authority before allowing any semantic C invocation.
            continuation = self._continuations.get(root)
            if (continuation is None or continuation.capture_basis != request.capture_basis
                or continuation.materiality_result_id != materiality.materiality_result_id
                or continuation.retrieval_operation_id != retrieval_id
                or continuation.attempt_id != attempt_id):
                raise InitialRcaRepairRequired("pre-retrieval durable identity is incomplete or contradictory")
            operation = self._candidate_c.read_operation(request.retrieval_request.operation_key)
            if operation.status is KnowledgeReadStatus.NOT_FOUND:
                knowledge = self._candidate_c.resolve(request.retrieval_request, request.retrieval_limits)
            elif operation.status is KnowledgeReadStatus.FOUND:
                read = operation.value
                if read.frozen.request != request.retrieval_request:
                    raise InitialRcaRepairRequired("Candidate-C frozen retrieval request contradicts D2 identity")
                if read.snapshot_key is None:
                    if (read.state is RetrievalOperationState.RETRIEVAL_COMPLETED
                        and isinstance(read.completion, DurableRetrievalCompletion)):
                        # C owns completion replay and derives the same Snapshot
                        # from its durable completion without querying upstream.
                        knowledge = self._candidate_c.resolve(request.retrieval_request, request.retrieval_limits)
                        if (knowledge.resolution not in (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH)
                            or knowledge.snapshot is None):
                            raise InitialRcaRepairRequired("Candidate-C completion replay did not produce a terminal Snapshot")
                        completed = self._candidate_c.read_operation(request.retrieval_request.operation_key)
                        if (completed.status is not KnowledgeReadStatus.FOUND
                            or completed.value.state is not RetrievalOperationState.COMPLETED
                            or completed.value.completion != read.completion
                            or completed.value.snapshot_key != knowledge.snapshot.snapshot_key):
                            raise InitialRcaRepairRequired("Candidate-C completion replay changed durable authority")
                    elif (read.state is RetrievalOperationState.FROZEN
                          and read.completion is None and read.recovery is None):
                        # A frozen operation is durable in-progress authority.
                        # C's public resolve resumes that exact frozen request.
                        knowledge = self._candidate_c.resolve(request.retrieval_request, request.retrieval_limits)
                        if knowledge.resolution in (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH):
                            if knowledge.snapshot is None:
                                raise InitialRcaRepairRequired("Candidate-C frozen replay lacks a terminal Snapshot")
                            completed = self._candidate_c.read_operation(request.retrieval_request.operation_key)
                            if (completed.status is not KnowledgeReadStatus.FOUND
                                or completed.value.frozen.request != read.frozen.request
                                or completed.value.state is not RetrievalOperationState.COMPLETED
                                or completed.value.snapshot_key != knowledge.snapshot.snapshot_key):
                                raise InitialRcaRepairRequired("Candidate-C frozen replay changed durable authority")
                    elif (read.state is RetrievalOperationState.TRANSIENT_UNAVAILABLE
                          and read.recovery is not None):
                        if request.unavailable_finalization_request is None:
                            return InitialRcaResult(InitialRcaDisposition.KNOWLEDGE_UNAVAILABLE, continuation, aggregate, evidence, materiality=materiality)
                        knowledge = self._candidate_c.finalize_unavailable(request.unavailable_finalization_request)
                    else:
                        raise InitialRcaRepairRequired("Candidate-C operation is partial or contradictory")
                else:
                    if read.state is not RetrievalOperationState.COMPLETED:
                        raise InitialRcaRepairRequired("Candidate-C Snapshot contradicts operation state")
                    snapshot_read = self._candidate_c.read_snapshot(read.snapshot_key)
                    if snapshot_read.status is not KnowledgeReadStatus.FOUND:
                        raise InitialRcaRepairRequired("Candidate-C terminal Snapshot cannot be read")
                    knowledge = KnowledgeResolutionOutcome(snapshot_read.value.resolution, snapshot_read.value)
            elif operation.status is KnowledgeReadStatus.UNAVAILABLE:
                return InitialRcaResult(InitialRcaDisposition.UNAVAILABLE, continuation, aggregate, evidence, materiality=materiality)
            else:
                raise InitialRcaRepairRequired("Candidate-C operation authority is invalid or unreadable")
            if knowledge.resolution in (RetrievalResolution.INVALID, RetrievalResolution.REPAIR_REQUIRED):
                return InitialRcaResult(InitialRcaDisposition.REPAIR_REQUIRED, continuation, aggregate, evidence, knowledge, materiality=materiality)
            if knowledge.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE and knowledge.snapshot is None:
                if request.unavailable_finalization_request is None:
                    return InitialRcaResult(InitialRcaDisposition.KNOWLEDGE_UNAVAILABLE, continuation, aggregate, evidence, knowledge, materiality=materiality)
                knowledge = self._candidate_c.finalize_unavailable(request.unavailable_finalization_request)
                if knowledge.resolution is not RetrievalResolution.RETRIEVAL_UNAVAILABLE or knowledge.snapshot is None:
                    raise InitialRcaRepairRequired("Candidate-C unavailable finalization is not terminal authority")
            if knowledge.snapshot is None:
                return InitialRcaResult(InitialRcaDisposition.REPAIR_REQUIRED, continuation, aggregate, evidence, knowledge, materiality=materiality)
            if getattr(knowledge.snapshot, "operation_key", request.retrieval_request.operation_key) != request.retrieval_request.operation_key:
                raise InitialRcaRepairRequired("Candidate-C terminal Snapshot contradicts retrieval identity")
            lineage = request.attempt_lineage or AttemptLineage(
                continuation.attempt_id, aggregate.aggregate_id, evidence.snapshot_id, evidence.revision_id,
                knowledge.snapshot.snapshot_key.value, request.generation_provenance)
            if (lineage.attempt_id != continuation.attempt_id or lineage.aggregate_id != aggregate.aggregate_id
                or lineage.evidence_snapshot_id != evidence.snapshot_id or lineage.evidence_revision_id != evidence.revision_id
                or lineage.knowledge_snapshot_id != knowledge.snapshot.snapshot_key.value):
                raise InitialRcaRepairRequired("Attempt lineage contradicts immutable public B/C/A facts")
            admission_id = derive_attempt_admission_operation_id(root, lineage)
            continuation = self._advance(continuation, stage="ATTEMPT", next_action="ATTEMPT")
            existing_attempt = self._candidate_a.get_attempt_lineage(lineage.attempt_id)
            if existing_attempt is not None:
                if existing_attempt.attempt.lineage != lineage:
                    raise InitialRcaRepairRequired("Candidate-A existing Attempt contradicts exact S2 lineage")
                attempt = existing_attempt.attempt
            else:
                attempt = self._candidate_a.admit_attempt(AdmitAttemptRequest(admission_id, lineage, request.authoritative_now))
            if attempt.lineage != lineage:
                raise InitialRcaRepairRequired("Candidate-A Attempt admission returned contradictory lineage")
            continuation = self._advance(continuation, stage="EXECUTION", next_action="EXECUTION")
            return InitialRcaResult(InitialRcaDisposition.READY_FOR_TRY, continuation, aggregate, evidence, knowledge, attempt, materiality)
        except OSError:
            return InitialRcaResult(InitialRcaDisposition.UNAVAILABLE, continuation)
        except (InitialRcaError, RcaRepairRequiredError, RcaDomainError, RuntimeWorkStoreError,
                ValueError, TypeError, AttributeError):
            return InitialRcaResult(InitialRcaDisposition.REPAIR_REQUIRED, continuation)

    def _authorize_work(self, request: InitialRcaRequest, incident: IncidentRecord,
                        relationship: IncidentRcaRelationship, root: str) -> None:
        """Reconcile one SPEC-011 D2 RCA work before any B/C/A effect."""
        expected_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root)
        if request.runtime_work_id != expected_id:
            raise InitialRcaRepairRequired("caller Runtime work ID contradicts stable RCA work identity")
        event_ids = getattr(incident, "event_ids", ())
        event_id = event_ids[0] if isinstance(event_ids, tuple) and event_ids else None
        if not isinstance(event_id, str) or not event_id:
            raise InitialRcaRepairRequired("Incident has no authoritative Event for Runtime work")
        if relationship.current_version_id is not None:
            raise InitialRcaRepairRequired("Incident already has Current RCA; initial work is not admissible")

        # D2 absence is not obligation absence. Validate the Incident-bound A
        # authority before creating a missing work record.
        self._candidate_a.validate_local_readiness()
        aggregate = self._candidate_a.get_aggregate_by_incident(request.incident_id)
        expected_aggregate = rca_child_operation_id(root, request.incident_id, "AGGREGATE_IDENTITY")
        if aggregate is not None and (aggregate.incident_id != request.incident_id
                                      or aggregate.aggregate_id != expected_aggregate):
            raise InitialRcaRepairRequired("Candidate-A Aggregate contradicts stable RCA obligation")
        if aggregate is not None and self._candidate_a.get_current(aggregate.aggregate_id) is not None:
            raise InitialRcaRepairRequired("Candidate-A Current contradicts initial RCA obligation")
        candidates = self._candidate_a.enumerate_recovery_candidates()
        if any(candidate.aggregate_id == expected_aggregate
               and candidate.kind is not RecoveryCandidateKind.ATTEMPT_TRY_RECONCILIATION
               for candidate in candidates):
            raise InitialRcaRepairRequired("Candidate-A has non-initial recovery authority")
        existing_continuation = self._continuations.get(root)
        if existing_continuation is not None and existing_continuation.runtime_work_id != expected_id:
            raise InitialRcaRepairRequired("RCA continuation contradicts Runtime work identity")

        with SqliteRuntimeWorkStore(self._continuations.database_path) as work_store:
            enumeration = work_store.enumerate_all()
            if enumeration.isolated_corruptions:
                raise InitialRcaRepairRequired("D2 Runtime work enumeration is incomplete")
            for record in enumeration.records:
                if (record.work_kind is RuntimeWorkKind.RCA_INITIAL
                    and (record.incident_id == request.incident_id or record.operation_id == root)
                    and record.work_id != expected_id):
                    raise InitialRcaRepairRequired("contradictory RCA work binding exists")
            work = work_store.get(expected_id)
            if work is None:
                try:
                    work = work_store.create(RuntimeWorkRecord(
                        expected_id, RuntimeWorkKind.RCA_INITIAL, event_id, "INITIAL", "INITIAL",
                        0, request.retry_limit, RuntimeWorkStatus.OUTSTANDING,
                        request.authoritative_now, request.authoritative_now, request.authoritative_now,
                        incident_id=request.incident_id, operation_id=root,
                    ))
                except ContradictoryRuntimeWorkError:
                    # Another discoverer may have established the same work
                    # with a different observation time. Read the winner.
                    work = work_store.get(expected_id)
                    if work is None:
                        raise InitialRcaRepairRequired("concurrent RCA work cannot be read")
            if (work.work_id != expected_id or work.work_kind is not RuntimeWorkKind.RCA_INITIAL
                or work.incident_id != request.incident_id or work.operation_id != root
                or work.event_id != event_id or work.retry_limit != request.retry_limit
                or work.status is not RuntimeWorkStatus.OUTSTANDING):
                raise InitialRcaRepairRequired("D2 RCA work authority is contradictory or terminal")

    def _load_or_create(self, root: str, request: InitialRcaRequest) -> RcaContinuation:
        existing = self._continuations.get(root)
        if existing is not None:
            if existing.runtime_work_id != request.runtime_work_id or existing.incident_id != request.incident_id:
                raise InitialRcaRepairRequired("RCA root resolves to contradictory Runtime continuation")
            self._candidate_a.validate_local_readiness()
            aggregate = self._candidate_a.get_aggregate_by_incident(request.incident_id)
            if existing.aggregate_id is not None and (aggregate is None or aggregate.aggregate_id != existing.aggregate_id):
                raise InitialRcaRepairRequired("D2 Aggregate reference contradicts Candidate-A authority")
            if aggregate is not None:
                matching = tuple(candidate for candidate in self._candidate_a.enumerate_recovery_candidates()
                                 if candidate.aggregate_id == aggregate.aggregate_id)
                outstanding = tuple(candidate.attempt_id for candidate in matching
                                    if candidate.kind is RecoveryCandidateKind.ATTEMPT_TRY_RECONCILIATION)
                if (len(outstanding) > 1 or any(candidate.kind is not RecoveryCandidateKind.ATTEMPT_TRY_RECONCILIATION
                                                for candidate in matching)
                    or (outstanding and outstanding[0] != existing.attempt_id)):
                    raise InitialRcaRepairRequired("D2 Attempt reference contradicts Candidate-A recovery authority")
            if existing.attempt_id is not None:
                view = self._candidate_a.get_attempt_lineage(existing.attempt_id)
                if view is not None and (aggregate is None or view.attempt.lineage.aggregate_id != aggregate.aggregate_id):
                    raise InitialRcaRepairRequired("D2 Attempt reference contradicts Candidate-A Aggregate")
            return existing
        # D2 absence is not Candidate-A absence. Reconstruct only after A has
        # validated and exposed its Aggregate and outstanding Attempt facts.
        self._candidate_a.validate_local_readiness()
        aggregate = self._candidate_a.get_aggregate_by_incident(request.incident_id)
        expected_aggregate_id = rca_child_operation_id(root, request.incident_id, "AGGREGATE_IDENTITY")
        if aggregate is not None and (aggregate.incident_id != request.incident_id
                                      or aggregate.aggregate_id != expected_aggregate_id):
            raise InitialRcaRepairRequired("Candidate-A Aggregate contradicts stable Incident root")
        candidates = self._candidate_a.enumerate_recovery_candidates()
        attempts = []
        if aggregate is not None:
            for candidate in candidates:
                if candidate.aggregate_id != aggregate.aggregate_id:
                    continue
                if candidate.kind is not RecoveryCandidateKind.ATTEMPT_TRY_RECONCILIATION:
                    raise InitialRcaRepairRequired("Candidate-A Aggregate has non-initial recovery authority")
                view = self._candidate_a.get_attempt_lineage(candidate.attempt_id)
                if view is None or view.attempt.lineage.aggregate_id != aggregate.aggregate_id:
                    raise InitialRcaRepairRequired("Candidate-A recovery Attempt is unreadable or contradictory")
                attempts.append(view.attempt)
        proposed_id = request.attempt_lineage.attempt_id if request.attempt_lineage else request.attempt_id
        proposed = self._candidate_a.get_attempt_lineage(proposed_id)
        if proposed is not None and (aggregate is None or proposed.attempt.lineage.aggregate_id != aggregate.aggregate_id
                                     or proposed.attempt not in attempts):
            raise InitialRcaRepairRequired("Candidate-A Attempt is outside coherent initial recovery authority")
        if len(attempts) > 1 or len({item.lineage.attempt_id for item in attempts}) != len(attempts):
            raise InitialRcaRepairRequired("Candidate-A has multiple outstanding Attempts for the root")
        recovered_attempt_id = attempts[0].lineage.attempt_id if attempts else None
        if request.attempt_lineage is not None and recovered_attempt_id is not None:
            if request.attempt_lineage != attempts[0].lineage:
                raise InitialRcaRepairRequired("Candidate-A Attempt contradicts supplied pinned lineage")
        record = RcaContinuation(root, request.runtime_work_id, request.incident_id, "INITIAL", "INITIAL",
                                 request.retry_limit, request.authoritative_now, request.authoritative_now,
                                 request.authoritative_now, aggregate_id=aggregate.aggregate_id if aggregate else None,
                                 attempt_id=recovered_attempt_id)
        try:
            return self._continuations.create(record)
        except (RcaContinuationConcurrencyError, RcaRepairRequiredError):
            # A competing durable worker may have created and advanced this
            # root between our authoritative read and create.  Fresh-read it;
            # only an equivalent stable binding is a legal convergence.
            existing = self._continuations.get(root)
            if (existing is None or existing.runtime_work_id != request.runtime_work_id
                    or existing.incident_id != request.incident_id):
                raise
            return self._load_or_create(root, request)

    def _aggregate(self, root: str, request: InitialRcaRequest) -> RcaAggregate:
        self._candidate_a.validate_local_readiness()
        aggregate = self._candidate_a.get_aggregate_by_incident(request.incident_id)
        aggregate_id = rca_child_operation_id(root, request.incident_id, "AGGREGATE_IDENTITY")
        if aggregate is not None:
            if aggregate.incident_id != request.incident_id or aggregate.aggregate_id != aggregate_id:
                raise InitialRcaRepairRequired("Candidate-A Aggregate binding is contradictory")
            return aggregate
        operation_id = rca_child_operation_id(root, aggregate_id, "AGGREGATE_DISCOVERY")
        aggregate = self._candidate_a.create_or_discover_aggregate(CreateAggregateRequest(operation_id, aggregate_id, request.incident_id, request.authoritative_now))
        if aggregate.incident_id != request.incident_id or aggregate.aggregate_id != aggregate_id:
            raise InitialRcaRepairRequired("Candidate-A Aggregate discovery returned contradictory binding")
        return aggregate

    def _freeze_capture_basis(self, continuation: RcaContinuation,
                              request: InitialRcaRequest) -> RcaContinuation:
        try:
            frozen = self._continuations.freeze_capture_basis(
                continuation.root_id, request.capture_basis, observed_at=request.authoritative_now,
                expected_revision=continuation.revision,
            )
        except RcaContinuationConcurrencyError:
            # Only the expected freeze CAS conflict permits a fresh read. The
            # winner's entire semantic basis must equal our original intent.
            frozen = self._continuations.get(continuation.root_id)
        if (frozen is None or frozen.root_id != continuation.root_id
            or frozen.incident_id != request.incident_id
            or frozen.runtime_work_id != request.runtime_work_id
            or frozen.aggregate_id != continuation.aggregate_id
            or frozen.capture_basis != request.capture_basis
            or frozen.capture_operation_id != request.capture_command.capture_operation_id):
            raise InitialRcaRepairRequired("D2 frozen Capture Command Basis contradicts request")
        return frozen

    def _evidence(self, request: InitialRcaRequest, continuation: RcaContinuation) -> CaptureTerminalOutcome | None:
        frozen = self._continuations.require_capture_command_basis(continuation.root_id)
        if frozen != request.capture_basis or continuation.capture_operation_id != request.capture_command.capture_operation_id:
            raise InitialRcaRepairRequired("Candidate-B command contradicts durable Capture Command Basis")
        existing = self._candidate_b.read_capture_outcome(request.capture_command.capture_operation_id)
        expected_semantics = capture_command_semantic_identity(request.capture_command)
        if existing is not None and (existing.capture_operation_id != request.capture_command.capture_operation_id
                                     or existing.command_semantic_identity != expected_semantics):
            raise InitialRcaRepairRequired("Candidate-B capture receipt contradicts frozen command semantics")
        outcome = existing if existing is not None else self._candidate_b.capture_evidence(request.capture_command)
        if isinstance(outcome, NonTerminalInvocationFailure):
            return None
        if not isinstance(outcome, CaptureTerminalOutcome):
            raise InitialRcaRepairRequired("Candidate-B public service returned an invalid outcome")
        if (outcome.capture_operation_id != request.capture_command.capture_operation_id
            or outcome.command_semantic_identity != expected_semantics):
            raise InitialRcaRepairRequired("Candidate-B outcome contradicts frozen capture operation")
        if outcome.terminal_kind is CaptureTerminalKind.SUCCESS:
            snapshot = self._candidate_b.resolve_snapshot(outcome.snapshot_id)
            revision = self._candidate_b.resolve_revision(outcome.revision_id)
            if (snapshot is None or revision is None
                or getattr(snapshot, "snapshot_id", None) != outcome.snapshot_id
                or getattr(snapshot, "revision_id", None) != outcome.revision_id
                or getattr(snapshot, "incident_id", None) != request.incident_id
                or getattr(snapshot, "capture_operation_id", outcome.capture_operation_id) != outcome.capture_operation_id
                or getattr(revision, "revision_id", None) != outcome.revision_id
                or getattr(revision, "incident_id", None) != request.incident_id):
                raise InitialRcaRepairRequired("Candidate-B Snapshot or Revision authority is incomplete or contradictory")
        return outcome

    def _advance(self, value: RcaContinuation, **changes: object) -> RcaContinuation:
        now = value.updated_at
        candidate = replace(value, **changes, updated_at=now, observed_at=now)
        try:
            return self._continuations.update(candidate, expected_revision=value.revision)
        except (RcaContinuationConcurrencyError, RcaRepairRequiredError):
            # CAS conflict is never a license to overwrite.  A fresh durable
            # read can converge only when every requested fact is already the
            # same (or the authoritative state has progressed beyond it).
            current = self._continuations.get(value.root_id)
            if current is None:
                raise
            for field, expected in changes.items():
                if field in {"stage", "next_action"}:
                    continue  # Progress may already be durably farther ahead.
                actual = getattr(current, field)
                if actual is not None and actual != expected:
                    raise InitialRcaRepairRequired("concurrent continuation fact contradicts authority")
            return current
