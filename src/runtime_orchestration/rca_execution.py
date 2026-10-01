"""SPEC-016 S3: one Attempt-scoped execution step over public A/D and D2 facts."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib

from llm_generation.contracts import GenerationInput, LocalReadStatus
from llm_generation.facade import CandidateDHandoffFacade
from llm_generation.service import (AvailableResources, ExecutionAuthorization,
    GenerationService, same_try_reinvocation_eligible)
from llm_generation.sqlite_store import CandidateDStore
from rca_persistence.contracts import (AdmittedRetryDisposition, AttemptLineageRead,
    GenerationLifecycle, LogicalTryIdentity, LogicalTryOutcome, LogicalTryResultKind,
    PublicationTargetIdentity, RcaDomainError, VersionRole)

from .clock import RuntimeClock
from .contracts import RuntimeWorkKind, RuntimeWorkStatus, RuntimeWorkStore
from .identity import runtime_operation_id, runtime_work_id
from .rca_continuation import SqliteRcaContinuationStore, RcaRepairRequiredError, rca_root_id
from .retry import DurableRetryController, RuntimeRetryIntegrityError
from .rca_publication import advance_publication_continuation
from .sqlite_work_store import RuntimeWorkConcurrencyError, RuntimeWorkStoreError


class RcaExecutionDisposition(str, Enum):
    ARTIFACT_COMMITTED = "ARTIFACT_COMMITTED"
    RETRY_PENDING = "RETRY_PENDING"
    EXHAUSTED = "EXHAUSTED"
    NON_RETRYABLE = "NON_RETRYABLE"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"
    OBSOLETE = "OBSOLETE"


@dataclass(frozen=True, slots=True)
class RcaExecutionRequest:
    incident_id: str
    attempt_id: str
    source: GenerationInput
    config: object
    resources: AvailableResources
    degraded_authorization: object | None = None


@dataclass(frozen=True, slots=True)
class RcaExecutionResult:
    disposition: RcaExecutionDisposition
    retry_slots_used: int = 0
    next_eligibility_at: object | None = None
    source_disposition: AdmittedRetryDisposition | None = None
    version: object | None = None


class RcaAttemptExecutor:
    """One step performs at most one D physical invocation; scheduling stays in D2."""

    def __init__(self, *, candidate_a: object, candidate_d: GenerationService,
                 d_store: CandidateDStore, handoff: CandidateDHandoffFacade,
                 work_store: RuntimeWorkStore, retry: DurableRetryController,
                 clock: RuntimeClock,
                 continuations: SqliteRcaContinuationStore | None = None) -> None:
        self._a, self._d, self._ds = candidate_a, candidate_d, d_store
        self._handoff, self._work, self._retry, self._clock = handoff, work_store, retry, clock
        self._continuations = continuations

    def step(self, request: RcaExecutionRequest) -> RcaExecutionResult:
        try:
            return self._step(request)
        except (RcaDomainError, RcaRepairRequiredError, RuntimeRetryIntegrityError, RuntimeWorkStoreError,
                ValueError, TypeError, AttributeError):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
        except OSError:
            return RcaExecutionResult(RcaExecutionDisposition.UNAVAILABLE)

    def _step(self, request: RcaExecutionRequest) -> RcaExecutionResult:
        root = rca_root_id(request.incident_id)
        self._a.validate_local_readiness()
        view = self._a.get_attempt_lineage(request.attempt_id)
        if not isinstance(view, AttemptLineageRead):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
        lineage = view.attempt.lineage
        if (lineage.attempt_id != request.attempt_id
            or request.source.try_identity.attempt_id != request.attempt_id
            or (lineage.evidence_snapshot_id, lineage.evidence_revision_id,
                lineage.knowledge_snapshot_id) !=
                (request.source.evidence_snapshot_id, request.source.evidence_revision_id,
                 request.source.knowledge_snapshot_id)
            or not request.source.pin.matches_attempt(lineage.generation_provenance)):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
        aggregate = self._a.get_aggregate(lineage.aggregate_id)
        if aggregate is None or aggregate.incident_id != request.incident_id:
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
        initial_id = runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root)
        initial = self._work.get(initial_id)
        if (initial is None or initial.work_kind is not RuntimeWorkKind.RCA_INITIAL
            or initial.incident_id != request.incident_id or initial.operation_id != root):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
        active = initial
        if initial.status is not RuntimeWorkStatus.OUTSTANDING:
            if self._continuations is None:
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            state = self._continuations.get(root)
            if (state is None or state.incident_id != request.incident_id
                or state.follow_up_work_id is None):
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            active = self._continuations.get_follow_up_work(root)
            if (active.work_id != state.follow_up_work_id
                or active.work_kind is not RuntimeWorkKind.RCA_FOLLOW_UP
                or active.status is not RuntimeWorkStatus.OUTSTANDING):
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
        current = self._a.get_current(lineage.aggregate_id)
        if current is not None and current.current.freshness.value != "STALE":
            return RcaExecutionResult(RcaExecutionDisposition.OBSOLETE)
        # Startup recovery fences prior executors through this existing D2
        # revision. Capture it for fresh checks before every later mutation.
        budget_id = runtime_work_id(RuntimeWorkKind.RCA_ATTEMPT, root, request.attempt_id)
        if self._work.get(budget_id) is None:
            # An absent D2 budget can be established only before any A/D Try
            # fact. Otherwise consumed slots cannot be reconstructed safely.
            recovery = self._ds.recovery()
            if recovery.status is not LocalReadStatus.FOUND or view.try_outcomes:
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            if (any(item.try_identity.attempt_id == request.attempt_id
                    for item in recovery.value.results)
                or any(item.try_identity.attempt_id == request.attempt_id
                       for item in recovery.value.failures)):
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
        budget = self._retry.establish_rca_attempt_budget(root_id=root,
            attempt_id=request.attempt_id, incident_id=request.incident_id,
            event_id=active.event_id)
        ordinal = len(view.try_outcomes) + 1
        if view.try_outcomes and view.try_outcomes[-1].result_kind is LogicalTryResultKind.VALIDATED_RESULT:
            ordinal = view.try_outcomes[-1].identity.try_ordinal
        elif view.try_outcomes and view.try_outcomes[-1].retry_disposition is not AdmittedRetryDisposition.RETRYABLE:
            disposition = view.try_outcomes[-1].retry_disposition
            return RcaExecutionResult(
                RcaExecutionDisposition.REPAIR_REQUIRED
                if disposition is AdmittedRetryDisposition.REPAIR_REQUIRED
                else RcaExecutionDisposition.NON_RETRYABLE,
                budget.attempt_count, source_disposition=disposition)
        identity = LogicalTryIdentity(request.attempt_id, ordinal)
        result_read = self._ds.result_for_try(identity)
        failure_read = self._ds.failures_for_try(identity)
        if result_read.status is LocalReadStatus.FOUND:
            if failure_read.status not in (LocalReadStatus.NOT_FOUND, LocalReadStatus.FOUND):
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            result_source = result_read.value.content.input
            if (budget.stage == "INITIAL" or budget.status not in (
                RuntimeWorkStatus.OUTSTANDING, RuntimeWorkStatus.COMPLETED)
                or result_source.operation_id not in {
                    self._source(request, root, ordinal, slot).operation_id
                    for slot in range(budget.attempt_count + 1)}
                or (ordinal > 1 and budget.attempt_count == 0)):
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            return self._handoff_result(request, root, view, result_read.value, budget, active)
        if result_read.status is not LocalReadStatus.NOT_FOUND or failure_read.status not in (
            LocalReadStatus.NOT_FOUND, LocalReadStatus.FOUND):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)

        if failure_read.status is LocalReadStatus.FOUND:
            failure = self._latest_failure(request, root, identity, budget.attempt_count,
                                           failure_read.value)
            if failure is None:
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            if budget.stage in {"INITIAL_INVOKING", "RETRY_INVOKING"}:
                self._require_execution_authority(active, budget)
                budget = self._retry.schedule_rca_failure(budget, failure)
            if failure.retry_safety is AdmittedRetryDisposition.REPAIR_REQUIRED:
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED,
                    budget.attempt_count, source_disposition=failure.retry_safety)
            if failure.retry_safety is AdmittedRetryDisposition.NON_RETRYABLE:
                self._require_execution_authority(active, budget)
                self._record_failure(root, identity, failure)
                return RcaExecutionResult(RcaExecutionDisposition.NON_RETRYABLE,
                    budget.attempt_count, source_disposition=failure.retry_safety)
            if budget.status is RuntimeWorkStatus.EXHAUSTED:
                self._require_execution_authority(active, budget)
                self._record_failure(root, identity, failure)
                return RcaExecutionResult(RcaExecutionDisposition.EXHAUSTED,
                    budget.attempt_count, source_disposition=failure.retry_safety)
            if not same_try_reinvocation_eligible(
                self._source(request, root, ordinal, budget.attempt_count + 1), self._ds, self._a):
                self._require_execution_authority(active, budget)
                self._record_failure(root, identity, failure)
                view = self._a.get_attempt_lineage(request.attempt_id)
                if not isinstance(view, AttemptLineageRead) or view.try_outcomes[-1].identity != identity:
                    return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
                ordinal += 1
                identity = LogicalTryIdentity(request.attempt_id, ordinal)

        if budget.status is RuntimeWorkStatus.EXHAUSTED:
            return RcaExecutionResult(RcaExecutionDisposition.EXHAUSTED,
                budget.attempt_count, source_disposition=AdmittedRetryDisposition.RETRYABLE)
        if budget.status is RuntimeWorkStatus.FAILED_CLOSED:
            disposition = AdmittedRetryDisposition(budget.source_retry_disposition)
            return RcaExecutionResult(RcaExecutionDisposition.NON_RETRYABLE
                if disposition is AdmittedRetryDisposition.NON_RETRYABLE
                else RcaExecutionDisposition.REPAIR_REQUIRED,
                budget.attempt_count, source_disposition=disposition)

        if budget.stage == "RETRY_PENDING":
            if budget.next_retry_at is None:
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            if self._clock.now() < budget.next_retry_at:
                return RcaExecutionResult(RcaExecutionDisposition.RETRY_PENDING,
                    budget.attempt_count, budget.next_retry_at, AdmittedRetryDisposition.RETRYABLE)
            # Recheck A/D immediately before consuming a shared slot.
            fresh = self._a.get_attempt_lineage(request.attempt_id)
            if not isinstance(fresh, AttemptLineageRead) or fresh.attempt.lineage != lineage:
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            if fresh.try_outcomes and fresh.try_outcomes[-1].identity.try_ordinal >= ordinal:
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            if self._ds.result_for_try(identity).status is not LocalReadStatus.NOT_FOUND:
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            try:
                self._require_execution_authority(active, budget)
                budget = self._retry.consume_rca_retry_slot(budget)
            except RuntimeWorkConcurrencyError:
                return RcaExecutionResult(RcaExecutionDisposition.RETRY_PENDING)
        elif budget.stage == "INITIAL":
            if ordinal != 1 or failure_read.status is not LocalReadStatus.NOT_FOUND:
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED)
            try:
                self._require_execution_authority(active, budget)
                budget = self._retry.mark_rca_initial_invoking(budget)
            except RuntimeWorkConcurrencyError:
                return RcaExecutionResult(RcaExecutionDisposition.RETRY_PENDING)
        else:
            # A consumed slot with no D receipt is ambiguous; never reset it.
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED,
                                      budget.attempt_count)

        source = self._source(request, root, ordinal, budget.attempt_count)
        fresh = self._a.get_attempt_lineage(request.attempt_id)
        durable_budget = self._work.get(budget.work_id)
        fresh_failures = self._ds.failures_for_try(identity)
        if (not isinstance(fresh, AttemptLineageRead) or fresh.attempt.lineage != lineage
            or any(out.identity == identity for out in fresh.try_outcomes)
            or durable_budget != budget
            or self._work.get(initial_id) != initial
            or self._ds.result_for_try(identity).status is not LocalReadStatus.NOT_FOUND
            or fresh_failures.status not in (LocalReadStatus.NOT_FOUND, LocalReadStatus.FOUND)
            or self._a.get_current(lineage.aggregate_id) != current):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, budget.attempt_count)
        if ordinal == 1 and fresh.attempt.lifecycle not in (
            GenerationLifecycle.PENDING, GenerationLifecycle.GENERATING):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, budget.attempt_count)
        if ordinal > 1 and (fresh.attempt.lifecycle is not GenerationLifecycle.FAILED
            or not fresh.try_outcomes
            or fresh.try_outcomes[-1].retry_disposition is not AdmittedRetryDisposition.RETRYABLE):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, budget.attempt_count)
        if ordinal > 1:
            previous = LogicalTryIdentity(request.attempt_id, ordinal - 1)
            previous_result = self._ds.result_for_try(previous)
            previous_failures = self._ds.failures_for_try(previous)
            if (previous_result.status is not LocalReadStatus.NOT_FOUND
                or previous_failures.status is not LocalReadStatus.FOUND
                or not any(item.failure_class.value == fresh.try_outcomes[-1].failure_code
                           and item.retry_safety is AdmittedRetryDisposition.RETRYABLE
                           for item in previous_failures.value)):
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED,
                                          budget.attempt_count)
        if fresh_failures.status is LocalReadStatus.FOUND and not same_try_reinvocation_eligible(
            source, self._ds, self._a):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, budget.attempt_count)
        grant = ExecutionAuthorization(source.operation_id, identity, len(fresh.try_outcomes), True)
        self._require_execution_authority(active, budget)
        receipt = self._d.execute(source, request.config, grant, request.resources,
                                  degraded_authorization=request.degraded_authorization)
        self._require_execution_authority(active, budget)
        if receipt.status is not LocalReadStatus.FOUND:
            return RcaExecutionResult(RcaExecutionDisposition.UNAVAILABLE
                if receipt.status is LocalReadStatus.UNAVAILABLE
                else RcaExecutionDisposition.REPAIR_REQUIRED, budget.attempt_count)
        if receipt.result is not None:
            return self._handoff_result(request, root, fresh, receipt.result, budget, active)
        if receipt.failure is None:
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, budget.attempt_count)
        updated = self._retry.schedule_rca_failure(budget, receipt.failure)
        if receipt.failure.retry_safety is AdmittedRetryDisposition.NON_RETRYABLE:
            self._require_execution_authority(active, updated)
            self._record_failure(root, identity, receipt.failure)
            return RcaExecutionResult(RcaExecutionDisposition.NON_RETRYABLE,
                updated.attempt_count, source_disposition=receipt.failure.retry_safety)
        if receipt.failure.retry_safety is AdmittedRetryDisposition.REPAIR_REQUIRED:
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED,
                updated.attempt_count, source_disposition=receipt.failure.retry_safety)
        if updated.status is RuntimeWorkStatus.EXHAUSTED:
            self._require_execution_authority(active, updated)
            self._record_failure(root, identity, receipt.failure)
            return RcaExecutionResult(RcaExecutionDisposition.EXHAUSTED,
                updated.attempt_count, source_disposition=receipt.failure.retry_safety)
        return RcaExecutionResult(RcaExecutionDisposition.RETRY_PENDING,
            updated.attempt_count, updated.next_retry_at, receipt.failure.retry_safety)

    @staticmethod
    def _source(request: RcaExecutionRequest, root: str, ordinal: int, slot: int) -> GenerationInput:
        return replace(request.source, try_identity=LogicalTryIdentity(request.attempt_id, ordinal),
            operation_id=runtime_operation_id("RCA_D_EXECUTION", root, request.attempt_id,
                                              str(ordinal), str(slot)))

    def _latest_failure(self, request: RcaExecutionRequest, root: str,
                        identity: LogicalTryIdentity, slot: int, failures: tuple) -> object | None:
        operation = self._source(request, root, identity.try_ordinal, slot).operation_id
        matches = [item for item in failures if item.try_identity == identity
                   and item.operation_id == operation]
        return matches[0] if len(matches) == 1 else None

    def _record_failure(self, root: str, identity: LogicalTryIdentity, failure: object) -> None:
        outcome = LogicalTryOutcome(identity, LogicalTryResultKind.FAILURE,
            failure.retry_safety, self._clock.now(), failure_code=failure.failure_class.value,
            safe_failure_message=failure.safe_detail)
        operation = runtime_operation_id("RCA_TRY_FAILURE", root, identity.attempt_id,
                                         str(identity.try_ordinal), failure.operation_id)
        persisted = self._a.record_try_outcome(operation, outcome)
        fresh = self._a.get_attempt_lineage(identity.attempt_id)
        if persisted != outcome or not isinstance(fresh, AttemptLineageRead) or fresh.try_outcomes[-1] != outcome:
            raise RuntimeRetryIntegrityError("Candidate-A failure outcome was not durable")

    def _handoff_result(self, request: RcaExecutionRequest, root: str,
                        view: AttemptLineageRead, result: object, budget: object,
                        active: object) -> RcaExecutionResult:
        used = budget.attempt_count
        source = result.content.input
        if source.try_identity.attempt_id != request.attempt_id:
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, used)
        projection = self._handoff.resolve(result.validated_result_id)
        if (projection.status is not LocalReadStatus.FOUND or projection.result != result
            or projection.artifact is None):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, used)
        identity = source.try_identity
        outcomes = view.try_outcomes
        if any(item.identity == identity for item in outcomes):
            if outcomes[-1].result_kind is not LogicalTryResultKind.VALIDATED_RESULT or outcomes[-1].validated_result_id != result.validated_result_id:
                return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, used)
        else:
            outcome = LogicalTryOutcome(identity, LogicalTryResultKind.VALIDATED_RESULT,
                AdmittedRetryDisposition.NON_RETRYABLE, self._clock.now(),
                validated_result_id=result.validated_result_id)
            operation = runtime_operation_id("RCA_TRY_SUCCESS", root, request.attempt_id,
                                             result.validated_result_id)
            self._require_execution_authority(active, budget)
            self._a.record_try_outcome(operation, outcome)
        fresh = self._a.get_attempt_lineage(request.attempt_id)
        if (not isinstance(fresh, AttemptLineageRead) or not fresh.try_outcomes
            or fresh.try_outcomes[-1].validated_result_id != result.validated_result_id
            or fresh.attempt.lifecycle is not GenerationLifecycle.COMPLETED):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, used)
        digest = hashlib.sha256((root + ":" + result.validated_result_id).encode()).hexdigest()
        version_id = "version:" + digest
        current = self._a.get_current(fresh.attempt.lineage.aggregate_id)
        expected_current = None if current is None else current.current.current_version_id
        target = PublicationTargetIdentity(
            runtime_operation_id("RCA_PUBLICATION_INTENT", root, version_id),
            fresh.attempt.lineage.aggregate_id, request.incident_id, version_id, expected_current)
        operation = runtime_operation_id("RCA_ARTIFACT_COMMIT", root, result.validated_result_id)
        self._require_execution_authority(active, budget)
        version = self._a.commit_validated_artifact(operation, request.attempt_id,
            projection.artifact, target, self._clock.now())
        if (version.version_id != version_id or version.artifact != projection.artifact
            or version.publication_operation_id != target.publication_operation_id
            or version.role is not VersionRole.COMMITTED_UNPUBLISHED):
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, used)
        self._require_execution_authority(active, budget)
        receipt = self._a.get_publication_result(version.publication_operation_id)
        if receipt is None or receipt.target != target:
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, used)
        if self._continuations is not None:
            advance_publication_continuation(self._a, self._continuations,
                receipt.target, self._clock.now(), expected_work=active)
        self._require_execution_authority(active, budget)
        terminal = self._work.complete(budget.work_id, observed_at=self._clock.now(),
                                      expected_revision=budget.revision)
        if terminal.status is not RuntimeWorkStatus.COMPLETED:
            return RcaExecutionResult(RcaExecutionDisposition.REPAIR_REQUIRED, used)
        return RcaExecutionResult(RcaExecutionDisposition.ARTIFACT_COMMITTED,
                                  used, version=version)

    def _require_execution_authority(self, active: object, budget: object) -> None:
        if (self._work.get(active.work_id) != active
            or active.status is not RuntimeWorkStatus.OUTSTANDING
            or self._work.get(budget.work_id) != budget):
            raise RuntimeRetryIntegrityError("RCA execution lost D2 fencing authority")
