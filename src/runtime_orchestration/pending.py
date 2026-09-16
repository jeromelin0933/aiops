"""Phase 4 Pending scheduling and authoritative reevaluation composition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Callable, Mapping, Protocol

from alert_correlation import (
    AlertCorrelationPolicyEngine,
    CorrelationEvaluationContext,
    CorrelationEvaluationFailure,
    CorrelationEvaluationSuccess,
    DecisionType,
    EvaluationPhase,
    IncidentCorrelationView,
)
from alert_correlation.state import (
    ActivePendingRecord,
    PendingPolicyUnavailableError,
    PendingStateService,
    ProcessingClaim,
    ResolvedCorrelationState,
    ResolvedState,
)

from .clock import RuntimeClock
from .intake import DurableEventIntake
from .orchestrator import RuntimeLifecycleState
from .reconciliation import (
    InitialCorrelationOrchestrator,
    InitialExecutionResult,
    RuntimeReadinessPort,
)
from .telemetry import NullRuntimeTelemetry, RuntimeTelemetry, RuntimeTelemetryEvent


class PendingStatePort(Protocol):
    def recovery_event_ids(self) -> tuple[str, ...]: ...
    def resolve(self, event_id: str) -> ResolvedCorrelationState: ...
    def acquire_claim(self, event_id: str) -> ProcessingClaim | None: ...
    def release_claim(self, claim: ProcessingClaim) -> None: ...


class IncidentViewsPort(Protocol):
    def list_correlation_views(self) -> tuple[IncidentCorrelationView, ...]: ...


class PendingOutcomeKind(str, Enum):
    ENTERED_PENDING = "ENTERED_PENDING"
    REMAINS_PENDING = "REMAINS_PENDING"
    TERMINAL_PROCESSED = "TERMINAL_PROCESSED"
    BLOCKED_EVALUATION_FAILURE = "BLOCKED_EVALUATION_FAILURE"
    BLOCKED_POLICY_UNAVAILABLE = "BLOCKED_POLICY_UNAVAILABLE"
    BLOCKED_PRESERVED = "BLOCKED_PRESERVED"
    CLAIM_BUSY = "CLAIM_BUSY"
    NO_LONGER_PENDING = "NO_LONGER_PENDING"
    EVENT_MISSING = "EVENT_MISSING"
    FAILED_CLOSED = "FAILED_CLOSED"


@dataclass(frozen=True, slots=True)
class PendingEventOutcome:
    event_id: str
    outcome_kind: PendingOutcomeKind
    evaluation_phase: EvaluationPhase | None = None
    pending: ActivePendingRecord | None = None
    terminal: InitialExecutionResult | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class PendingSweepResult:
    candidate_event_ids: tuple[str, ...]
    outcomes: tuple[PendingEventOutcome, ...]


class RuntimePendingError(RuntimeError):
    pass


class PendingOrchestrator:
    """Enumerate and reevaluate every active Pending Event once per sweep."""

    def __init__(
        self,
        *,
        event_intake: DurableEventIntake,
        state_store: PendingStatePort,
        pending_service: PendingStateService,
        policy_engine: AlertCorrelationPolicyEngine,
        incident_store: IncidentViewsPort,
        terminal_executor: InitialCorrelationOrchestrator,
        clock: RuntimeClock,
        readiness: RuntimeReadinessPort,
        telemetry: RuntimeTelemetry | None = None,
    ) -> None:
        if not isinstance(event_intake, DurableEventIntake):
            raise TypeError("event_intake must be DurableEventIntake")
        if not isinstance(pending_service, PendingStateService):
            raise TypeError("pending_service must be the real SPEC-007 service")
        if not isinstance(policy_engine, AlertCorrelationPolicyEngine):
            raise TypeError("policy_engine must be the real SPEC-006 engine")
        if not isinstance(terminal_executor, InitialCorrelationOrchestrator):
            raise TypeError("terminal_executor must be the Phase 3 executor")
        if not isinstance(clock, RuntimeClock):
            raise TypeError("clock must be RuntimeClock")
        if not isinstance(getattr(readiness, "state", None), RuntimeLifecycleState):
            raise TypeError("readiness must expose RuntimeLifecycleState")
        for owner, methods in (
            (state_store, ("recovery_event_ids", "resolve", "acquire_claim", "release_claim")),
            (incident_store, ("list_correlation_views",)),
        ):
            if any(not callable(getattr(owner, method, None)) for method in methods):
                raise TypeError("Pending dependency lacks a required public API")
        self._event_intake = event_intake
        self._state_store = state_store
        self._pending_service = pending_service
        self._policy_engine = policy_engine
        self._incident_store = incident_store
        self._terminal_executor = terminal_executor
        self._clock = clock
        self._readiness = readiness
        self._telemetry = telemetry or NullRuntimeTelemetry()

    def process_authoritative_event(self, event_id: str) -> PendingEventOutcome:
        """Composite INITIAL dispatcher for RuntimeBootstrap execution."""
        return self.enter_initial_pending(event_id)

    def enter_initial_pending(self, event_id: str) -> PendingEventOutcome:
        """Evaluate one authoritative UNSEEN Event and accept ENTER_PENDING."""
        self._require_ready()
        event = self._authoritative_event(event_id)
        claim = self._state_store.acquire_claim(event_id)
        if claim is None:
            self._emit_claim(event_id, "REFUSED")
            return PendingEventOutcome(event_id, PendingOutcomeKind.CLAIM_BUSY)
        self._emit_claim(event_id, "ACQUIRED")
        resolved = self._state_store.resolve(event_id)
        if resolved.state is not ResolvedState.UNSEEN or resolved.claim != claim:
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            return PendingEventOutcome(event_id, PendingOutcomeKind.NO_LONGER_PENDING)
        result = self._evaluate(event, CorrelationEvaluationContext(EvaluationPhase.INITIAL))
        if isinstance(result, CorrelationEvaluationFailure):
            self._pending_service.record_evaluation_failure(
                result,
                EvaluationPhase.INITIAL,
                now=self._clock.now(),
                claim=claim,
            )
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            return PendingEventOutcome(
                event_id,
                PendingOutcomeKind.BLOCKED_EVALUATION_FAILURE,
                EvaluationPhase.INITIAL,
            )
        decision = result.decision
        if decision.decision_type is not DecisionType.ENTER_PENDING:
            terminal = self._terminal_executor.execute_authoritative_terminal_decision(
                event_id, decision, claim
            )
            return PendingEventOutcome(
                event_id,
                PendingOutcomeKind.TERMINAL_PROCESSED,
                EvaluationPhase.INITIAL,
                terminal=terminal,
            )
        pending = self._pending_service.accept_enter_pending(
            event_id, decision, now=self._clock.now(), claim=claim
        )
        self._state_store.release_claim(claim)
        self._emit_claim(event_id, "RELEASED")
        return PendingEventOutcome(
            event_id,
            PendingOutcomeKind.ENTERED_PENDING,
            EvaluationPhase.INITIAL,
            pending=pending,
        )

    def sweep_once(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> PendingSweepResult:
        """Process every member of one deterministic Pending candidate set."""
        self._require_ready()
        stop_requested = should_stop or (lambda: False)
        if not callable(stop_requested):
            raise TypeError("should_stop must be callable")
        candidate_ids = self._pending_event_ids()
        snapshot = self._event_intake.scan_authoritative()
        outcomes: list[PendingEventOutcome] = []
        for event_id in candidate_ids:
            if stop_requested():
                break
            event = snapshot.event_by_id.get(event_id)
            if event is None:
                outcomes.append(
                    PendingEventOutcome(event_id, PendingOutcomeKind.EVENT_MISSING)
                )
                continue
            try:
                outcomes.append(self._reevaluate_one(event_id, event))
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                outcomes.append(
                    PendingEventOutcome(
                        event_id,
                        PendingOutcomeKind.FAILED_CLOSED,
                        error_type=type(exc).__name__,
                    )
                )
        return PendingSweepResult(candidate_ids, tuple(outcomes))

    def recover_authoritative_intent(self, event_id: str) -> InitialExecutionResult:
        """Resume exactly the durable Intent discovered by startup recovery."""
        self._require_recovery()
        resolved = self._state_store.resolve(event_id)
        if resolved.intent is None or resolved.claim is None:
            raise RuntimePendingError(
                "startup Intent recovery lacks authoritative Intent and claim"
            )
        return self._terminal_executor.resume_authoritative_intent(
            event_id, resolved.claim
        )

    def recover_expired_pending(self, event_id: str) -> PendingEventOutcome:
        """Perform required expired-Pending reevaluation before READY."""
        self._require_recovery()
        event = self._authoritative_event(event_id)
        return self._reevaluate_one(event_id, event)

    def _pending_event_ids(self) -> tuple[str, ...]:
        recovery_ids = self._state_store.recovery_event_ids()
        if (
            not isinstance(recovery_ids, tuple)
            or tuple(sorted(set(recovery_ids))) != recovery_ids
        ):
            raise RuntimePendingError(
                "SPEC-007 Pending enumeration is not deterministic and unique"
            )
        pending_ids = []
        for event_id in recovery_ids:
            resolved = self._state_store.resolve(event_id)
            if resolved.pending is not None:
                pending_ids.append(event_id)
        return tuple(pending_ids)

    def _reevaluate_one(
        self, event_id: str, event: Mapping[str, object]
    ) -> PendingEventOutcome:
        claim = self._state_store.acquire_claim(event_id)
        if claim is None:
            self._emit_claim(event_id, "REFUSED")
            return PendingEventOutcome(event_id, PendingOutcomeKind.CLAIM_BUSY)
        self._emit_claim(event_id, "ACQUIRED")
        resolved = self._state_store.resolve(event_id)
        if resolved.pending is None or resolved.claim != claim:
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            return PendingEventOutcome(event_id, PendingOutcomeKind.NO_LONGER_PENDING)
        if resolved.blocked is not None:
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            return PendingEventOutcome(
                event_id,
                PendingOutcomeKind.BLOCKED_PRESERVED,
                resolved.blocked.evaluation_phase,
                pending=resolved.pending,
            )

        now = self._clock.now()
        try:
            phase = self._pending_service.resolve_pending_phase(
                event_id, now=now, claim=claim
            )
        except PendingPolicyUnavailableError:
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            blocked = self._state_store.resolve(event_id).blocked
            return PendingEventOutcome(
                event_id,
                PendingOutcomeKind.BLOCKED_POLICY_UNAVAILABLE,
                blocked.evaluation_phase if blocked is not None else None,
                pending=resolved.pending,
            )

        evaluation = self._evaluate(event, phase.context)
        if isinstance(evaluation, CorrelationEvaluationFailure):
            self._pending_service.record_evaluation_failure(
                evaluation,
                phase.evaluation_phase,
                now=now,
                claim=claim,
            )
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            return PendingEventOutcome(
                event_id,
                PendingOutcomeKind.BLOCKED_EVALUATION_FAILURE,
                phase.evaluation_phase,
                pending=phase.pending,
            )

        decision = evaluation.decision
        if decision.decision_type is DecisionType.ENTER_PENDING:
            pending = self._pending_service.accept_enter_pending(
                event_id, decision, now=now, claim=claim
            )
            self._state_store.release_claim(claim)
            self._emit_claim(event_id, "RELEASED")
            return PendingEventOutcome(
                event_id,
                PendingOutcomeKind.REMAINS_PENDING,
                phase.evaluation_phase,
                pending=pending,
            )

        terminal = self._terminal_executor.execute_authoritative_terminal_decision(
            event_id, decision, claim
        )
        return PendingEventOutcome(
            event_id,
            PendingOutcomeKind.TERMINAL_PROCESSED,
            phase.evaluation_phase,
            terminal=terminal,
        )

    def _evaluate(
        self,
        event: Mapping[str, object],
        context: CorrelationEvaluationContext,
    ) -> CorrelationEvaluationSuccess | CorrelationEvaluationFailure:
        views = self._incident_store.list_correlation_views()
        if not isinstance(views, tuple) or any(
            not isinstance(view, IncidentCorrelationView) for view in views
        ):
            raise RuntimePendingError(
                "SPEC-008 returned malformed IncidentCorrelationViews"
            )
        result = self._policy_engine.evaluate(event, views, context)
        if not isinstance(
            result, (CorrelationEvaluationSuccess, CorrelationEvaluationFailure)
        ):
            raise RuntimePendingError("SPEC-006 returned an unsupported result")
        return result

    def _authoritative_event(self, event_id: str) -> Mapping[str, object]:
        event = self._event_intake.scan_authoritative().event_by_id.get(event_id)
        if event is None:
            raise RuntimePendingError(
                f"Event {event_id} is absent from authoritative EventStore"
            )
        return event

    def _require_ready(self) -> None:
        if self._readiness.state is not RuntimeLifecycleState.READY:
            raise RuntimePendingError(
                "Pending processing requires a completed Startup Recovery Barrier"
            )

    def _require_recovery(self) -> None:
        if self._readiness.state is not RuntimeLifecycleState.RECOVERY:
            raise RuntimePendingError(
                "startup recovery operation requires the active Recovery Barrier"
            )

    def _emit_claim(self, event_id: str, result: str) -> None:
        self._telemetry.emit(
            RuntimeTelemetryEvent.WORK_UPDATED,
            observed_at=self._clock.now(),
            event_id=event_id,
            stage="CLAIM",
            claim_result=result,
        )


class PendingSweepScheduler:
    """Process-local cadence gate; no durable tick or wall-clock semantics."""

    def __init__(
        self,
        orchestrator: PendingOrchestrator,
        *,
        cadence_seconds: float,
        monotonic: Callable[[], float],
    ) -> None:
        if not isinstance(orchestrator, PendingOrchestrator):
            raise TypeError("orchestrator must be PendingOrchestrator")
        if (
            isinstance(cadence_seconds, bool)
            or not isinstance(cadence_seconds, (int, float))
            or not math.isfinite(cadence_seconds)
            or cadence_seconds <= 0
        ):
            raise ValueError("cadence_seconds must be positive and finite")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._orchestrator = orchestrator
        self._cadence_seconds = float(cadence_seconds)
        self._monotonic = monotonic
        self._next_due = monotonic()

    @property
    def next_due_monotonic(self) -> float:
        return self._next_due

    def poll(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> PendingSweepResult | None:
        now = self._monotonic()
        if now < self._next_due:
            return None
        result = self._orchestrator.sweep_once(should_stop=should_stop)
        self._next_due = now + self._cadence_seconds
        return result
