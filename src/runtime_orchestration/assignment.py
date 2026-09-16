"""Phase 5 post-correlation AUTO_ASSIGN obligation and recovery."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from alert_correlation.state import (
    ProcessedCorrelationRecord,
    ResolvedCorrelationState,
    TerminalOutcome,
)
from incident_management import (
    AssignmentPolicyConfig,
    IncidentManager,
    IncidentRecord,
    IncidentStatus,
    WorkflowAction,
    WorkflowAuditEntry,
    WorkflowDomainError,
    WorkflowErrorCode,
    WorkflowMutationRequest,
    WorkflowOperationResult,
)

from .clock import RuntimeClock
from .contracts import (
    RuntimeWorkEnumeration,
    RuntimeWorkKind,
    RuntimeWorkRecord,
    RuntimeWorkStatus,
    RuntimeWorkStore,
)
from .identity import automatic_assignment_operation_id, runtime_work_id
from .retry import DurableRetryController
from .telemetry import NullRuntimeTelemetry, RuntimeTelemetry, RuntimeTelemetryEvent


class AssignmentStatePort(Protocol):
    def recovery_event_ids(self) -> tuple[str, ...]: ...
    def resolve(self, event_id: str) -> ResolvedCorrelationState: ...


class AssignmentIncidentReadPort(Protocol):
    def get_incident(self, incident_id: str) -> IncidentRecord | None: ...
    def event_has_incident_owner(self, event_id: str) -> bool: ...
    def get_workflow_operation_result(
        self, workflow_operation_id: str
    ) -> WorkflowOperationResult | None: ...
    def list_workflow_audit(
        self, incident_id: str
    ) -> tuple[WorkflowAuditEntry, ...]: ...


class AssignmentMutationPort(Protocol):
    def auto_assign_incident(
        self, request: WorkflowMutationRequest, policy: AssignmentPolicyConfig
    ) -> WorkflowOperationResult: ...


class AutoAssignOutcomeKind(str, Enum):
    ASSIGNED = "ASSIGNED"
    RECEIPT_RECONCILED = "RECEIPT_RECONCILED"
    SUPPRESSED_DOMAIN_ADVANCED = "SUPPRESSED_DOMAIN_ADVANCED"
    RETRY_OUTSTANDING = "RETRY_OUTSTANDING"
    TERMINAL_FAILURE_PRESERVED = "TERMINAL_FAILURE_PRESERVED"


@dataclass(frozen=True, slots=True)
class AutoAssignOutcome:
    event_id: str
    incident_id: str
    workflow_operation_id: str
    outcome_kind: AutoAssignOutcomeKind
    work: RuntimeWorkRecord | None = None
    workflow_result: WorkflowOperationResult | None = None


@dataclass(frozen=True, slots=True)
class AutoAssignRecoveryResult:
    outcomes: tuple[AutoAssignOutcome, ...]


class AutoAssignIntegrityError(RuntimeError):
    pass


class AutoAssignBudgetExhaustedError(RuntimeError):
    pass


class AutoAssignOrchestrator:
    """Coordinate D2 continuity while SPEC-009 owns assignment semantics."""

    def __init__(
        self,
        *,
        state_store: AssignmentStatePort,
        incident_store: AssignmentIncidentReadPort,
        incident_manager: AssignmentMutationPort,
        work_store: RuntimeWorkStore,
        assignment_policy: AssignmentPolicyConfig,
        automation_actor: str,
        retry_delays_seconds: tuple[float, ...],
        clock: RuntimeClock,
        telemetry: RuntimeTelemetry | None = None,
    ) -> None:
        if not isinstance(assignment_policy, AssignmentPolicyConfig):
            raise TypeError("assignment_policy must be the real SPEC-009 type")
        if (
            not isinstance(automation_actor, str)
            or not automation_actor
            or automation_actor != automation_actor.strip()
        ):
            raise ValueError("automation_actor must be a stable non-empty reference")
        if not isinstance(clock, RuntimeClock):
            raise TypeError("clock must be RuntimeClock")
        if not retry_delays_seconds or any(
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or delay <= 0
            for delay in retry_delays_seconds
        ):
            raise ValueError("retry_delays_seconds must contain positive delays")
        for owner, methods in (
            (state_store, ("recovery_event_ids", "resolve")),
            (
                incident_store,
                (
                    "get_incident",
                    "event_has_incident_owner",
                    "get_workflow_operation_result",
                    "list_workflow_audit",
                ),
            ),
            (incident_manager, ("auto_assign_incident",)),
            (work_store, ("create", "get", "enumerate_all", "update", "complete")),
        ):
            if any(not callable(getattr(owner, method, None)) for method in methods):
                raise TypeError("AUTO_ASSIGN dependency lacks a required public API")
        self._state_store = state_store
        self._incident_store = incident_store
        self._incident_manager = incident_manager
        self._work_store = work_store
        self._assignment_policy = assignment_policy
        self._automation_actor = automation_actor
        self._retry_delays = tuple(float(delay) for delay in retry_delays_seconds)
        self._clock = clock
        self._telemetry = telemetry or NullRuntimeTelemetry()
        self._retry = DurableRetryController(
            work_store=work_store,
            retry_delays_seconds=self._retry_delays,
            clock=clock,
            telemetry=telemetry,
        )

    def handle_processed_created_incident(
        self, processed: ProcessedCorrelationRecord
    ) -> AutoAssignOutcome:
        """Reconcile or execute one post-correlation assignment obligation."""
        self._require_created_incident(processed)
        incident_id = processed.incident_id
        if incident_id is None:
            raise AutoAssignIntegrityError("CREATED_INCIDENT has no Incident destination")
        workflow_id = automatic_assignment_operation_id(
            incident_id, self._automation_actor
        )
        work_id = runtime_work_id(
            RuntimeWorkKind.AUTO_ASSIGN, processed.event_id, incident_id
        )
        work = self._work_store.get(work_id)
        if work is not None:
            self._validate_work(work, processed, workflow_id)

        incident = self._authoritative_destination(processed)
        receipt = self._incident_store.get_workflow_operation_result(workflow_id)
        if receipt is not None:
            self._verify_receipt(receipt, incident_id)
            current = self._authoritative_destination(processed)
            self._verify_receipt_state(receipt, current)
            completed = self._complete_if_outstanding(work)
            outcome = AutoAssignOutcome(
                processed.event_id,
                incident_id,
                workflow_id,
                AutoAssignOutcomeKind.RECEIPT_RECONCILED,
                completed,
                receipt,
            )
            self._emit_outcome(outcome)
            return outcome

        if not self._is_open_unassigned(incident):
            completed = self._complete_if_outstanding(work)
            outcome = AutoAssignOutcome(
                processed.event_id,
                incident_id,
                workflow_id,
                AutoAssignOutcomeKind.SUPPRESSED_DOMAIN_ADVANCED,
                completed,
            )
            self._emit_outcome(outcome)
            return outcome

        if work is not None and work.status in {
            RuntimeWorkStatus.FAILED_CLOSED,
            RuntimeWorkStatus.EXHAUSTED,
        }:
            outcome = AutoAssignOutcome(
                processed.event_id,
                incident_id,
                workflow_id,
                AutoAssignOutcomeKind.TERMINAL_FAILURE_PRESERVED,
                work,
            )
            self._emit_outcome(outcome)
            return outcome

        work = self._ensure_outstanding_work(processed, workflow_id, work)
        if work.attempt_count >= work.retry_limit:
            raise AutoAssignBudgetExhaustedError(work.work_id)
        now = self._clock.now()
        if work.next_retry_at is not None and now < work.next_retry_at:
            outcome = AutoAssignOutcome(
                processed.event_id,
                incident_id,
                workflow_id,
                AutoAssignOutcomeKind.RETRY_OUTSTANDING,
                work,
            )
            self._emit_outcome(outcome)
            return outcome
        request = WorkflowMutationRequest(
            workflow_id,
            incident_id,
            self._automation_actor,
            now,
            WorkflowAction.AUTO_ASSIGN,
        )
        self._telemetry.emit(
            RuntimeTelemetryEvent.WORK_OBSERVED,
            observed_at=now,
            event_id=processed.event_id,
            stage="DOMAIN_ATTEMPT",
            workflow_operation_id=workflow_id,
            incident_id=incident_id,
            source_domain="SPEC-009",
            attempt=work.attempt_count + 1,
        )
        try:
            returned = self._incident_manager.auto_assign_incident(
                request, self._assignment_policy
            )
        except WorkflowDomainError as exc:
            current = self._authoritative_destination(processed)
            if (
                exc.code is WorkflowErrorCode.ASSIGNMENT_NOT_ALLOWED
                and not self._is_open_unassigned(current)
            ):
                completed = self._work_store.complete(
                    work.work_id,
                    observed_at=self._clock.now(),
                    expected_revision=work.revision,
                )
                outcome = AutoAssignOutcome(
                    processed.event_id,
                    incident_id,
                    workflow_id,
                    AutoAssignOutcomeKind.SUPPRESSED_DOMAIN_ADVANCED,
                    completed,
                )
                self._emit_outcome(outcome)
                return outcome
            self._record_failure(work, exc)
            raise

        receipt = self._incident_store.get_workflow_operation_result(workflow_id)
        if receipt is None or returned != receipt:
            raise AutoAssignIntegrityError(
                "SPEC-009 success has no matching authoritative workflow result"
            )
        self._verify_receipt(receipt, incident_id)
        self._verify_receipt_state(receipt, self._authoritative_destination(processed))
        completed = self._work_store.complete(
            work.work_id,
            observed_at=self._clock.now(),
            expected_revision=work.revision,
        )
        outcome = AutoAssignOutcome(
            processed.event_id,
            incident_id,
            workflow_id,
            AutoAssignOutcomeKind.ASSIGNED,
            completed,
            receipt,
        )
        self._emit_outcome(outcome)
        return outcome

    def recover_all(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> AutoAssignRecoveryResult:
        """Discover obligations from Processed authority, never correlation replay."""
        stop_requested = should_stop or (lambda: False)
        if not callable(stop_requested):
            raise TypeError("should_stop must be callable")
        work = self._work_store.enumerate_all()
        if not isinstance(work, RuntimeWorkEnumeration):
            raise AutoAssignIntegrityError("D2 returned an invalid enumeration")
        if work.isolated_corruptions:
            raise AutoAssignIntegrityError(
                "corrupt D2 work cannot safely preserve retry continuity"
            )
        processed_by_event: dict[str, ProcessedCorrelationRecord] = {}
        for event_id in self._state_store.recovery_event_ids():
            resolved = self._state_store.resolve(event_id)
            processed = resolved.processed
            if (
                processed is not None
                and processed.terminal_outcome is TerminalOutcome.CREATED_INCIDENT
            ):
                processed_by_event[event_id] = processed

        for record in work.records:
            if record.work_kind is not RuntimeWorkKind.AUTO_ASSIGN:
                continue
            processed = processed_by_event.get(record.event_id)
            if processed is None or processed.incident_id != record.incident_id:
                raise AutoAssignIntegrityError(
                    "D2 AUTO_ASSIGN work has no matching Processed destination"
                )

        outcomes: list[AutoAssignOutcome] = []
        for event_id in sorted(processed_by_event):
            if stop_requested():
                break
            processed = processed_by_event[event_id]
            try:
                outcomes.append(self.handle_processed_created_incident(processed))
            except WorkflowDomainError:
                incident_id = processed.incident_id
                if incident_id is None:
                    raise AutoAssignIntegrityError(
                        "CREATED_INCIDENT has no Incident destination"
                    )
                current = self._work_store.get(
                    runtime_work_id(RuntimeWorkKind.AUTO_ASSIGN, event_id, incident_id)
                )
                if current is None:
                    raise AutoAssignIntegrityError(
                        "typed AUTO_ASSIGN failure did not preserve D2 continuity"
                    )
                outcome = AutoAssignOutcome(
                    event_id,
                    incident_id,
                    automatic_assignment_operation_id(
                        incident_id, self._automation_actor
                    ),
                    (
                        AutoAssignOutcomeKind.RETRY_OUTSTANDING
                        if current.status is RuntimeWorkStatus.OUTSTANDING
                        else AutoAssignOutcomeKind.TERMINAL_FAILURE_PRESERVED
                    ),
                    current,
                )
                self._emit_outcome(outcome)
                outcomes.append(outcome)
        return AutoAssignRecoveryResult(tuple(outcomes))

    def _emit_outcome(self, outcome: AutoAssignOutcome) -> None:
        self._telemetry.emit(
            RuntimeTelemetryEvent.RECONCILIATION,
            observed_at=self._clock.now(),
            event_id=outcome.event_id,
            stage="AUTO_ASSIGN",
            workflow_operation_id=outcome.workflow_operation_id,
            incident_id=outcome.incident_id,
            attempt=getattr(outcome.work, "attempt_count", None),
            retry_disposition=getattr(
                outcome.work, "source_retry_disposition", None
            ),
            reconciliation_result=outcome.outcome_kind,
        )

    @staticmethod
    def _require_created_incident(processed: ProcessedCorrelationRecord) -> None:
        if not isinstance(processed, ProcessedCorrelationRecord):
            raise TypeError("processed must be a SPEC-007 ProcessedCorrelationRecord")
        if processed.terminal_outcome is not TerminalOutcome.CREATED_INCIDENT:
            raise AutoAssignIntegrityError(
                "only CREATED_INCIDENT creates an AUTO_ASSIGN obligation"
            )

    def _authoritative_destination(
        self, processed: ProcessedCorrelationRecord
    ) -> IncidentRecord:
        incident = self._incident_store.get_incident(processed.incident_id)
        has_owner = self._incident_store.event_has_incident_owner(processed.event_id)
        if (
            not isinstance(incident, IncidentRecord)
            or has_owner is not True
            or processed.event_id not in incident.event_ids
        ):
            raise AutoAssignIntegrityError(
                "Processed destination contradicts authoritative Incident ownership"
            )
        return incident

    @staticmethod
    def _is_open_unassigned(incident: IncidentRecord) -> bool:
        return incident.status is IncidentStatus.OPEN and incident.assignee is None

    def _ensure_outstanding_work(
        self,
        processed: ProcessedCorrelationRecord,
        workflow_id: str,
        work: RuntimeWorkRecord | None,
    ) -> RuntimeWorkRecord:
        if work is not None:
            if work.status is not RuntimeWorkStatus.OUTSTANDING:
                raise AutoAssignIntegrityError(
                    "completed D2 work contradicts missing workflow receipt"
                )
            return work
        now = self._clock.now()
        return self._work_store.create(
            RuntimeWorkRecord(
                work_id=runtime_work_id(
                    RuntimeWorkKind.AUTO_ASSIGN,
                    processed.event_id,
                    processed.incident_id,
                ),
                work_kind=RuntimeWorkKind.AUTO_ASSIGN,
                event_id=processed.event_id,
                incident_id=processed.incident_id,
                stage="OUTSTANDING",
                next_action="AUTO_ASSIGN",
                workflow_operation_id=workflow_id,
                attempt_count=0,
                retry_limit=self._retry.domain_attempt_limit,
                status=RuntimeWorkStatus.OUTSTANDING,
                created_at=now,
                updated_at=now,
                observed_at=now,
            )
        )

    @staticmethod
    def _validate_work(
        work: RuntimeWorkRecord,
        processed: ProcessedCorrelationRecord,
        workflow_id: str,
    ) -> None:
        if (
            work.work_kind is not RuntimeWorkKind.AUTO_ASSIGN
            or work.event_id != processed.event_id
            or work.incident_id != processed.incident_id
            or work.workflow_operation_id != workflow_id
        ):
            raise AutoAssignIntegrityError(
                "D2 AUTO_ASSIGN identity contradicts authoritative evidence"
            )

    @staticmethod
    def _verify_receipt(
        receipt: WorkflowOperationResult, incident_id: str
    ) -> None:
        if (
            not isinstance(receipt, WorkflowOperationResult)
            or receipt.incident_id != incident_id
            or receipt.action is not WorkflowAction.AUTO_ASSIGN
            or receipt.resulting_status is not IncidentStatus.ASSIGNED
        ):
            raise AutoAssignIntegrityError(
                "workflow receipt contradicts AUTO_ASSIGN obligation"
            )

    def _verify_receipt_state(
        self, receipt: WorkflowOperationResult, incident: IncidentRecord
    ) -> None:
        audits = self._incident_store.list_workflow_audit(incident.incident_id)
        if not isinstance(audits, tuple) or any(
            not isinstance(entry, WorkflowAuditEntry) for entry in audits
        ):
            raise AutoAssignIntegrityError(
                "SPEC-009 returned malformed workflow audit authority"
            )
        receipt_audits = tuple(
            entry
            for entry in audits
            if entry.workflow_operation_id == receipt.workflow_operation_id
        )
        if len(receipt_audits) != 1:
            raise AutoAssignIntegrityError(
                "workflow receipt lacks exactly one authoritative audit"
            )
        receipt_audit = receipt_audits[0]
        if (
            receipt_audit.incident_id != receipt.incident_id
            or receipt_audit.action is not WorkflowAction.AUTO_ASSIGN
            or receipt_audit.old_status is not IncidentStatus.OPEN
            or receipt_audit.new_status is not IncidentStatus.ASSIGNED
            or receipt_audit.occurred_at != receipt.completed_at
            or receipt_audit.assignment_policy_id != receipt.assignment_policy_id
            or receipt_audit.assignment_policy_version
            != receipt.assignment_policy_version
        ):
            raise AutoAssignIntegrityError(
                "workflow receipt contradicts authoritative workflow audit"
            )
        if (
            incident.updated_at < receipt.completed_at
            or incident.status is IncidentStatus.OPEN
            or incident.assignee is None
            or incident.reviewer is None
            or incident.reviewer != receipt.bound_reviewer
        ):
            raise AutoAssignIntegrityError(
                "workflow receipt contradicts authoritative Incident state"
            )

        later_audits = tuple(
            entry
            for entry in audits
            if entry.workflow_operation_id != receipt.workflow_operation_id
            and entry.occurred_at >= receipt.completed_at
        )
        if incident.assignee != receipt.selected_assignee and not any(
            entry.action is WorkflowAction.REASSIGN for entry in later_audits
        ):
            raise AutoAssignIntegrityError(
                "Incident assignee changed without authoritative reassignment evidence"
            )
        if incident.status is not IncidentStatus.ASSIGNED and not any(
            entry.new_status is incident.status for entry in later_audits
        ):
            raise AutoAssignIntegrityError(
                "Incident lifecycle advanced without authoritative workflow evidence"
            )

    def _complete_if_outstanding(
        self, work: RuntimeWorkRecord | None
    ) -> RuntimeWorkRecord | None:
        if work is None or work.status is RuntimeWorkStatus.COMPLETED:
            return work
        if work.status is not RuntimeWorkStatus.OUTSTANDING:
            raise AutoAssignIntegrityError("terminal D2 work cannot be reconciled")
        return self._work_store.complete(
            work.work_id,
            observed_at=self._clock.now(),
            expected_revision=work.revision,
        )

    def _record_failure(
        self, work: RuntimeWorkRecord, error: WorkflowDomainError
    ) -> RuntimeWorkRecord:
        return self._retry.record_domain_failure(
            work, error, source_domain="SPEC-009"
        )
