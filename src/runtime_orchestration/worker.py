"""Single-core Runtime loop with explicit termination and drain policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import math
from threading import Event
from typing import Protocol

from alert_correlation.state import ResolvedCorrelationState, RetryDisposition

from .assignment import AutoAssignOutcomeKind, AutoAssignOrchestrator
from .clock import RuntimeClock, canonical_utc
from .orchestrator import RuntimeBootstrap
from .pending import PendingOrchestrator, PendingSweepScheduler
from .recovery import RuntimeDispatchKind
from .retry import DurableRetryController
from .contracts import RuntimeWorkKind, RuntimeWorkRecord, RuntimeWorkStatus, RuntimeWorkStore
from .reconciliation import InitialCorrelationOrchestrator
from .identity import runtime_operation_id
from .telemetry import NullRuntimeTelemetry, RuntimeTelemetry, RuntimeTelemetryEvent


class RuntimeWorkerMode(str, Enum):
    RUN_UNTIL_IDLE = "run-until-idle"
    CONTINUOUS = "continuous"


class RuntimeWorkerExit(str, Enum):
    IDLE = "IDLE"
    DRAINED = "DRAINED"


@dataclass(frozen=True, slots=True)
class RuntimeCycleResult:
    acquired_work: int
    completed_safe_boundaries: int
    next_eligibility: datetime | None = None

    def __post_init__(self) -> None:
        for value in (self.acquired_work, self.completed_safe_boundaries):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("cycle counts must be non-negative integers")
        if self.completed_safe_boundaries > self.acquired_work:
            raise ValueError("safe boundaries cannot exceed acquired work")
        if self.next_eligibility is not None:
            object.__setattr__(
                self, "next_eligibility", canonical_utc(self.next_eligibility)
            )

    @property
    def made_progress(self) -> bool:
        return self.acquired_work > 0


class RuntimeWorkerCore(Protocol):
    def startup(self) -> None: ...
    def run_cycle(self) -> RuntimeCycleResult: ...


class RetryStatePort(Protocol):
    def resolve(self, event_id: str) -> ResolvedCorrelationState: ...


class AuthoritativeTerminalRetryExecutor:
    """Resume only the same durable Intent and operation identity."""

    def __init__(
        self,
        *,
        state_store: RetryStatePort,
        terminal_executor: InitialCorrelationOrchestrator,
        work_store: RuntimeWorkStore,
        clock: RuntimeClock,
    ) -> None:
        self._state_store = state_store
        self._terminal_executor = terminal_executor
        self._work_store = work_store
        self._clock = clock

    def execute_retry(self, work: RuntimeWorkRecord) -> RuntimeWorkRecord:
        if work.work_kind is not RuntimeWorkKind.DOMAIN_OPERATION:
            raise RuntimeRetryExecutionError("unsupported retry work kind")
        resolved = self._state_store.resolve(work.event_id)
        if resolved.processed is not None:
            return self._complete_current(work.work_id)
        if (
            resolved.intent is None
            or resolved.claim is None
            or resolved.intent.operation_id != work.operation_id
        ):
            raise RuntimeRetryExecutionError(
                "retry lacks matching authoritative Intent and claim"
            )
        self._terminal_executor.resume_authoritative_intent(
            work.event_id, resolved.claim, attempt=work.attempt_count + 1
        )
        return self._complete_current(work.work_id)

    def restore_before_ready(self, work: RuntimeWorkRecord) -> RuntimeWorkRecord:
        """Validate retry continuity and reconcile a committed receipt, if present."""
        if work.work_kind is not RuntimeWorkKind.DOMAIN_OPERATION:
            raise RuntimeRetryExecutionError("unsupported retry work kind")
        current = self._work_store.get(work.work_id)
        if current is None:
            raise RuntimeRetryExecutionError("retry work disappeared")
        if current.status is not RuntimeWorkStatus.OUTSTANDING:
            return current
        resolved = self._state_store.resolve(current.event_id)
        if resolved.processed is not None:
            return self._complete_current(current.work_id)
        if (
            resolved.intent is None
            or resolved.claim is None
            or resolved.intent.operation_id != current.operation_id
        ):
            raise RuntimeRetryExecutionError(
                "retry lacks matching authoritative Intent and claim"
            )
        reconciled = self._terminal_executor.reconcile_authoritative_intent_receipt(
            current.event_id, resolved.claim
        )
        if reconciled is not None:
            return self._complete_current(current.work_id)
        return current

    def _complete_current(self, work_id: str) -> RuntimeWorkRecord:
        current = self._work_store.get(work_id)
        if current is None:
            raise RuntimeRetryExecutionError("retry work disappeared")
        if current.status is RuntimeWorkStatus.COMPLETED:
            return current
        return self._work_store.complete(
            work_id,
            observed_at=self._clock.now(),
            expected_revision=current.revision,
        )


class RuntimeRetryExecutionError(RuntimeError):
    pass


class CorrelationRuntimeCore:
    """Compose authoritative intake, Pending and AUTO_ASSIGN in one cycle."""

    def __init__(
        self,
        *,
        bootstrap: RuntimeBootstrap,
        initial_dispatcher: PendingOrchestrator,
        pending_scheduler: PendingSweepScheduler,
        auto_assign: AutoAssignOrchestrator,
        retry: DurableRetryController,
        retry_executor: AuthoritativeTerminalRetryExecutor,
        clock: RuntimeClock,
        stop: RuntimeStopController,
        telemetry: RuntimeTelemetry | None = None,
    ) -> None:
        if not isinstance(bootstrap, RuntimeBootstrap):
            raise TypeError("bootstrap must be RuntimeBootstrap")
        if not isinstance(initial_dispatcher, PendingOrchestrator):
            raise TypeError("initial_dispatcher must be PendingOrchestrator")
        if not isinstance(pending_scheduler, PendingSweepScheduler):
            raise TypeError("pending_scheduler must be PendingSweepScheduler")
        if not isinstance(auto_assign, AutoAssignOrchestrator):
            raise TypeError("auto_assign must be AutoAssignOrchestrator")
        if not isinstance(retry, DurableRetryController):
            raise TypeError("retry must be DurableRetryController")
        if not isinstance(retry_executor, AuthoritativeTerminalRetryExecutor):
            raise TypeError("retry_executor must be AuthoritativeTerminalRetryExecutor")
        self._bootstrap = bootstrap
        self._initial_dispatcher = initial_dispatcher
        self._pending_scheduler = pending_scheduler
        self._auto_assign = auto_assign
        self._retry = retry
        self._retry_executor = retry_executor
        self._clock = clock
        self._stop = stop
        self._telemetry = telemetry or NullRuntimeTelemetry()

    def startup(self) -> None:
        self._telemetry.emit(
            RuntimeTelemetryEvent.STARTUP,
            observed_at=self._clock.now(),
            stage="STARTING",
        )
        snapshot = self._bootstrap.begin_recovery()
        retry_by_event = {
            work.event_id: work
            for work in snapshot.runtime_work.records
            if work.work_kind is RuntimeWorkKind.DOMAIN_OPERATION
        }
        for event_id in snapshot.events.event_by_id:
            self._telemetry.emit(
                RuntimeTelemetryEvent.WORK_OBSERVED,
                observed_at=self._clock.now(),
                event_id=event_id,
                stage="AUTHORITATIVE_EVENT_INTAKE",
                source_domain="SPEC-001",
            )
        for classification in snapshot.states.classifications:
            if classification.dispatch_kind is RuntimeDispatchKind.UNRESOLVED_INTENT:
                retry_work = retry_by_event.get(classification.event_id)
                try:
                    if retry_work is None:
                        self._initial_dispatcher.recover_authoritative_intent(
                            classification.event_id
                        )
                    else:
                        self._retry_executor.restore_before_ready(retry_work)
                except Exception as exc:
                    if not isinstance(
                        getattr(exc, "retry_disposition", None), RetryDisposition
                    ):
                        raise
            elif classification.dispatch_kind is RuntimeDispatchKind.EXPIRED_PENDING:
                outcome = self._initial_dispatcher.recover_expired_pending(
                    classification.event_id
                )
                self._emit_correlation_outcome(outcome, stage="PENDING_RECOVERY")
            elif classification.dispatch_kind in {
                RuntimeDispatchKind.ACTIVE_PENDING,
                RuntimeDispatchKind.ACTIVE_PENDING_BLOCKED,
                RuntimeDispatchKind.EXPIRED_PENDING_BLOCKED,
            }:
                self._telemetry.emit(
                    RuntimeTelemetryEvent.RECOVERY,
                    observed_at=self._clock.now(),
                    event_id=classification.event_id,
                    stage="PENDING_RESTORED",
                    next_eligibility=classification.pending_expires_at,
                    retry_disposition=classification.retry_disposition,
                    reconciliation_result=classification.dispatch_kind,
                )
        for work in snapshot.runtime_work.records:
            if (
                work.work_kind is RuntimeWorkKind.DOMAIN_OPERATION
                and work.event_id not in {
                    item.event_id
                    for item in snapshot.states.classifications
                    if item.dispatch_kind is RuntimeDispatchKind.UNRESOLVED_INTENT
                }
            ):
                self._retry_executor.restore_before_ready(work)
        assignment = self._auto_assign.recover_all(
            should_stop=lambda: self._stop.requested
        )
        for outcome in assignment.outcomes:
            self._telemetry.emit(
                RuntimeTelemetryEvent.RECOVERY,
                observed_at=self._clock.now(),
                event_id=outcome.event_id,
                stage="AUTO_ASSIGN_RESTORED",
                workflow_operation_id=outcome.workflow_operation_id,
                incident_id=outcome.incident_id,
                attempt=getattr(outcome.work, "attempt_count", None),
                retry_disposition=getattr(
                    outcome.work, "source_retry_disposition", None
                ),
                reconciliation_result=outcome.outcome_kind,
            )
        self._bootstrap._complete_recovery(snapshot)
        self._telemetry.emit(
            RuntimeTelemetryEvent.RECOVERY,
            observed_at=self._clock.now(),
            stage="RECOVERY_COMPLETE",
            reconciliation_result="REQUIRED_RECOVERY_COMPLETED",
        )

    def run_cycle(self) -> RuntimeCycleResult:
        acquired = completed = 0
        if self._stop.requested:
            return RuntimeCycleResult(0, 0)
        try:
            assignment = self._auto_assign.recover_all(
                should_stop=lambda: self._stop.requested
            )
        except Exception as exc:
            disposition = getattr(exc, "retry_disposition", None)
            if not isinstance(disposition, RetryDisposition):
                raise
            acquired += 1
            completed += 1
            self._telemetry.emit(
                RuntimeTelemetryEvent.WORK_UPDATED,
                observed_at=self._clock.now(),
                stage="DOMAIN_FAILURE_RECORDED",
                source_domain="SPEC-009",
                source_error_code=getattr(getattr(exc, "code", None), "value", None),
                retry_disposition=disposition,
            )
        else:
            for outcome in assignment.outcomes:
                if outcome.outcome_kind is AutoAssignOutcomeKind.ASSIGNED:
                    acquired += 1
                    completed += 1
                self._telemetry.emit(
                    RuntimeTelemetryEvent.RECONCILIATION,
                    observed_at=self._clock.now(),
                    event_id=outcome.event_id,
                    stage="AUTO_ASSIGN",
                    workflow_operation_id=outcome.workflow_operation_id,
                    incident_id=outcome.incident_id,
                    policy_id=getattr(
                        outcome.workflow_result, "assignment_policy_id", None
                    ),
                    policy_version=getattr(
                        outcome.workflow_result, "assignment_policy_version", None
                    ),
                    attempt=getattr(outcome.work, "attempt_count", None),
                    reconciliation_result=outcome.outcome_kind,
                )

        if self._stop.requested:
            eligibility = self._retry.enumerate_eligibility()
            return RuntimeCycleResult(acquired, completed, eligibility.next_eligibility)

        dispatch = self._bootstrap.dispatch_normal_intake()
        for classification in dispatch.classifications:
            if self._stop.requested:
                break
            if classification.dispatch_kind is not RuntimeDispatchKind.UNSEEN:
                continue
            self._telemetry.emit(
                RuntimeTelemetryEvent.WORK_OBSERVED,
                observed_at=self._clock.now(),
                event_id=classification.event_id,
                stage="AUTHORITATIVE_EVENT_INTAKE",
                source_domain="SPEC-001",
            )
            acquired += 1
            try:
                outcome = self._initial_dispatcher.process_authoritative_event(
                    classification.event_id
                )
            except Exception as exc:
                disposition = getattr(exc, "retry_disposition", None)
                if not isinstance(disposition, RetryDisposition):
                    raise
            else:
                self._emit_correlation_outcome(outcome, stage="INITIAL")
            completed += 1

        eligibility = self._retry.enumerate_eligibility()
        for work in eligibility.eligible:
            if self._stop.requested:
                break
            if work.work_kind is not RuntimeWorkKind.DOMAIN_OPERATION:
                continue
            acquired += 1
            try:
                self._retry_executor.execute_retry(work)
            except Exception as exc:
                if not isinstance(
                    getattr(exc, "retry_disposition", None), RetryDisposition
                ):
                    raise
            completed += 1

        pending = (
            None
            if self._stop.requested
            else self._pending_scheduler.poll(
                should_stop=lambda: self._stop.requested
            )
        )
        if pending is not None:
            acquired += len(pending.outcomes)
            completed += len(pending.outcomes)
            for outcome in pending.outcomes:
                self._emit_correlation_outcome(outcome, stage="PENDING")

        eligibility = self._retry.enumerate_eligibility()
        return RuntimeCycleResult(acquired, completed, eligibility.next_eligibility)

    def _emit_correlation_outcome(self, outcome: object, *, stage: str) -> None:
        terminal = getattr(outcome, "terminal", None)
        processed = getattr(terminal, "processed", None)
        decision = getattr(terminal, "decision", None)
        event_id = getattr(outcome, "event_id", None)
        self._telemetry.emit(
            RuntimeTelemetryEvent.WORK_COMPLETED,
            observed_at=self._clock.now(),
            event_id=event_id,
            stage=stage,
            decision=getattr(decision, "decision_type", None),
            policy_id=getattr(processed, "policy_id", None),
            policy_version=getattr(processed, "policy_version", None),
            operation_id=(
                runtime_operation_id("TERMINAL_CORRELATION", event_id)
                if processed is not None and isinstance(event_id, str)
                else None
            ),
            claim_result="SAFE_BOUNDARY",
            incident_id=getattr(processed, "incident_id", None),
            shadow_id=getattr(processed, "shadow_ref", None),
            reconciliation_result=getattr(outcome, "outcome_kind", None),
        )


class RuntimeStopController:
    def __init__(self) -> None:
        self._requested = Event()

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def request_stop(self) -> None:
        self._requested.set()


class RuntimeWorker:
    """Both modes execute the exact same synchronous safe-boundary cycle."""

    def __init__(
        self,
        core: RuntimeWorkerCore,
        *,
        clock: RuntimeClock,
        idle_poll_seconds: float,
        stop: RuntimeStopController | None = None,
        telemetry: RuntimeTelemetry | None = None,
    ) -> None:
        if any(
            not callable(getattr(core, method, None))
            for method in ("startup", "run_cycle")
        ):
            raise TypeError("core lacks the Runtime worker API")
        if not isinstance(clock, RuntimeClock):
            raise TypeError("clock must be RuntimeClock")
        if (
            isinstance(idle_poll_seconds, bool)
            or not isinstance(idle_poll_seconds, (int, float))
            or not math.isfinite(idle_poll_seconds)
            or idle_poll_seconds <= 0
        ):
            raise ValueError("idle_poll_seconds must be positive and finite")
        self._core = core
        self._clock = clock
        self._idle_poll_seconds = float(idle_poll_seconds)
        self._stop = stop or RuntimeStopController()
        self._telemetry = telemetry or NullRuntimeTelemetry()

    @property
    def stop_controller(self) -> RuntimeStopController:
        return self._stop

    def run(self, mode: RuntimeWorkerMode) -> RuntimeWorkerExit:
        if not isinstance(mode, RuntimeWorkerMode):
            raise TypeError("mode must be RuntimeWorkerMode")
        self._core.startup()
        self._telemetry.emit(
            RuntimeTelemetryEvent.READY,
            observed_at=self._clock.now(),
            stage="READY",
        )
        while True:
            if self._stop.requested:
                return self._drained()
            result = self._core.run_cycle()
            if not isinstance(result, RuntimeCycleResult):
                raise TypeError("core returned an invalid RuntimeCycleResult")
            # run_cycle is synchronous: a stop requested inside it takes effect
            # only after the acquired work reaches this durable safe boundary.
            if self._stop.requested:
                return self._drained()
            if mode is RuntimeWorkerMode.RUN_UNTIL_IDLE and not result.made_progress:
                return RuntimeWorkerExit.IDLE
            if not result.made_progress:
                delay = self._idle_poll_seconds
                if result.next_eligibility is not None:
                    delay = min(
                        delay,
                        max(0.0, (result.next_eligibility - self._clock.now()).total_seconds()),
                    )
                self._wait_monotonic(delay)

    def _wait_monotonic(self, delay: float) -> None:
        deadline = self._clock.monotonic() + delay
        remaining = deadline - self._clock.monotonic()
        if remaining > 0 and not self._stop.requested:
            self._clock.sleep(remaining)

    def _drained(self) -> RuntimeWorkerExit:
        self._telemetry.emit(
            RuntimeTelemetryEvent.STOP,
            observed_at=self._clock.now(),
            stage="DRAINED",
        )
        return RuntimeWorkerExit.DRAINED
